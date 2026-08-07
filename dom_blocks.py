"""Shared DOM → blocks walker for all source parsers.

WHY THIS MODULE EXISTS. Every source parser used to carry its own copy of the
inline-markup walker. All three defects found on 2026-07-28 lived in two of
those functions, and fixing them meant fixing the same bug in four files —
user-spec Risk 1, "carrying the code over together with its bugs". After the
extraction both functions live here once.

WHAT IS POLICY AND WHAT IS MECHANISM. The seam was measured on 14 real
articles (code-research.md Part II § II-1), not guessed. `runs_from_tag` has
exactly ONE genuine per-site hook (`has_color_class`), and the walk body
contains no site-specific line at all. Everything else — h2/h3/h4 → level 3,
h5 → paragraph, h1/h6 dropped, one <p> split at <br>, <li> without a bullet —
is GOOD policy already pinned by the orangetrack tests, and it becomes the
shared default rather than a parameter.

The per-site surface is deliberately tiny: four seams on ``BlockBuilder``
(junk-class predicate, image ``src`` picker, image dedup key, video-provider
DATA) plus the opt-in ``headings_from_bold``. ``has_color_class`` is the
``runs_from_tag`` seam threaded through the builder, not a fifth site knob.
Every default is conservative — "there are no colour classes", "there is no
junk class", dedup by stripping the query, heuristic off — so a source added
later behaves safely until someone measures its markup.

THE VIDEO SEAM IS DATA, NOT A CALLABLE (Decision 1). A source passes a host
allowlist and a provider name; the hostname check and the ID regex stay in
here and the host gate runs FIRST. If the wrapper were injectable,
autoevolution would hand in its regex-only path in Phase 2 and the extraction
would have LEGITIMISED that defect behind an interface.

This module is a leaf of the import graph: no parser, no publisher, no
news_bot, and no feature_flags. Per Decision 6 the feature flag must not gate
emission here — orangetrack is the first consumer of the shared module, and a
gate inside it would strip orangetrack's blocks when the flag is off, which
is a regression on a working source and an AC10 violation.
"""

import re
import urllib.parse
from typing import Dict, List, Optional
from urllib.parse import urlparse

# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Schemes that survive the href filter. Anything else (``javascript:``,
#: ``data:``, ``file:``, scheme-relative ``//evil/x``) drops the href and
#: the anchor degenerates to plain text.
_ALLOWED_HREF_SCHEMES = frozenset(("http", "https", "mailto"))

#: Schemes accepted on ``<img src>``. ``data:`` SVGs and ``file:`` paths
#: are dropped (defense-in-depth — Telegraph filters too, but the parser
#: must not emit them in the first place).
_ALLOWED_IMG_SCHEMES = frozenset(("http", "https"))

#: Inline-formatting tags that map to Telegraph-supported nodes. Order in
#: each value is ALSO the nesting order when multiple formats apply to the
#: same span (outer → inner): bold > italic > underline > strikethrough.
_INLINE_FORMAT_TAGS = {
    "strong": "bold",
    "b": "bold",
    "em": "italic",
    "i": "italic",
    "u": "underline",
    "s": "strikethrough",
    "del": "strikethrough",
}

#: Resource bounds applied at PARSE time (Decision 8). Past either one the
#: block keeps its full text and loses its runs — never truncated, never
#: dropped, never raised. A resource fuse, not the editorial "too much bold"
#: threshold that user-spec AC7 forbids.
#:
#: These are dom_blocks' OWN constants, deliberately not imported from
#: ``telegraph_publisher``: a parser must not depend on the renderer. The
#: repo already works this way — the publisher keeps its own copy of
#: ``_BOLD_MARKER_RE`` so it need not depend on the LLM-engine layer
#: (patterns.md, "Change one, change both"). The values match the render-path
#: bounds on purpose: runs the renderer would discard anyway are not worth
#: carrying, and a drift would produce blocks that travel with markers and
#: render flat.
MAX_TEXT_FOR_RUNS = 100_000
MAX_RUNS_PER_BLOCK = 100

#: Canonical host allowlists, published so consumers opt IN explicitly rather
#: than keeping a private copy each. Passing them stays the source's decision —
#: nothing here is a default — but a source that wants YouTube should not have
#: to retype the tuple, since a drifted copy is the exact failure mode this
#: module exists to end.
YOUTUBE_HOSTS = (
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
    "youtu.be",
)

#: Video-provider table. A source supplies only a host allowlist and a
#: provider NAME; the shapes below stay here so no source can inject a
#: wrapper that skips the hostname gate (Decision 1).
_VIDEO_PROVIDERS = {
    "youtube": {
        # Captures the ID from the common URL shapes. Bounded quantifier —
        # ReDoS contract, Decision 8.
        "id_re": re.compile(
            r"(?:youtu\.be/|/embed/|/watch\?v=|/v/|/shorts/)"
            r"([A-Za-z0-9_-]{6,})"
        ),
        "canonical": "https://www.youtube.com/watch?v={video_id}",
    },
    "vimeo": {
        "id_re": re.compile(r"(?:vimeo\.com/|/video/)(\d{6,})"),
        "canonical": "https://vimeo.com/{video_id}",
    },
}


# --------------------------------------------------------------------------- #
# URL safety                                                                  #
# --------------------------------------------------------------------------- #


def safe_href(href: Optional[str]) -> Optional[str]:
    """Return ``href`` iff it's a safe scheme; else None.

    Allowed: http, https, mailto (for editorial mailto links).
    Dropped: javascript:, data:, file:, scheme-relative ``//evil/x``,
    relative paths, malformed strings.
    """
    if not href or not isinstance(href, str):
        return None
    href = href.strip()
    if not href:
        return None
    # Scheme-relative URLs (``//evil/x``) have no parsed scheme; reject.
    if href.startswith("//"):
        return None
    try:
        parsed = urlparse(href)
    except (ValueError, AttributeError):
        return None
    scheme = (parsed.scheme or "").lower()
    if scheme in _ALLOWED_HREF_SCHEMES:
        return href
    return None


def safe_img_src(src: Optional[str]) -> Optional[str]:
    """Return ``src`` iff it's an http(s) URL; else None."""
    if not src or not isinstance(src, str):
        return None
    src = src.strip()
    if not src:
        return None
    if src.startswith("//"):
        return None
    try:
        parsed = urlparse(src)
    except (ValueError, AttributeError):
        return None
    if (parsed.scheme or "").lower() not in _ALLOWED_IMG_SCHEMES:
        return None
    return src


def video_embed_url(url: str, *, hosts, provider: str) -> Optional[str]:
    """Wrap a provider video URL into Telegra.ph's iframe-embed proxy form.

    THE HOSTNAME GATE RUNS BEFORE THE ID REGEX. Without it a non-provider URL
    merely CONTAINING ``youtube.com/embed/abc`` would be falsely wrapped —
    the gap autoevolution's regex-only path has. Keeping both the gate and
    the regex in here is the point of the data-only seam: a source hands in
    ``hosts`` and ``provider``, never a wrapper of its own.

    Returns ``None`` when the URL is not on the allowlist, the provider is
    unknown, or no ID can be extracted. Telegra.ph validates ``iframe.src``
    at create-page time and accepts ONLY ``/embed/<provider>?url=…`` proxy
    URLs, so a raw video URL would be silently stripped to an empty
    ``/embed/`` and Instant View would break.
    """
    if not isinstance(url, str) or not url:
        return None
    spec = _VIDEO_PROVIDERS.get(provider)
    if spec is None:
        return None
    try:
        parsed = urlparse(url)
    except (ValueError, AttributeError):
        return None
    host = (parsed.hostname or "").lower()
    if host not in hosts:
        return None
    m = spec["id_re"].search(url)
    if not m:
        return None
    canonical = spec["canonical"].format(video_id=m.group(1))
    return (
        f"https://telegra.ph/embed/{provider}?url="
        + urllib.parse.quote(canonical, safe="")
    )


# --------------------------------------------------------------------------- #
# Runs                                                                        #
# --------------------------------------------------------------------------- #


def _no_color_class(node) -> bool:
    """Conservative default for the colour-class seam: a source that has not
    been measured gets no colour→bold mapping at all."""
    return False


def text_from_runs(runs: List[Dict]) -> str:
    """Flatten ``runs`` into the block's ``text`` field.

    Joined with NO separator: each run already carries its own surrounding
    whitespace from the source HTML, so a `" ".join` inserted a SECOND space at
    every format boundary (`<p>Plain <strong>bold</strong> tail.</p>` flattened
    to ``'Plain  bold  tail.'``). Fixed 2026-07-28 — four call sites had drifted
    into three different spellings of this join, only one of which collapsed.
    In the shared module there is exactly one spelling.

    Runs are preserved verbatim by the caller; only this flattened `text` field
    is normalised, so ``text.find(run_text)`` in
    ``telegraph_publisher._render_paragraph_with_runs`` still locates every run.
    """
    return re.sub(r"\s+", " ", "".join(r["text"] for r in runs)).strip()


def runs_from_tag(tag, *, has_color_class=_no_color_class) -> List[Dict]:
    """Walk a tag's contents and return ordered runs.

    Run shape: ``{'text': str, ['href': str], ['formats': list[str]]}``.

    * Anchors (``<a>``) with safe schemes preserve the ``href``;
      anchors with dropped/unsafe href degenerate to plain text.
    * Inline formatting tags (``<strong>``, ``<b>``, ``<em>``, ``<i>``,
      ``<u>``, ``<s>``, ``<del>``) and any element the injected
      ``has_color_class`` predicate accepts accumulate format markers in the
      ``formats`` list (cumulative across nested elements). Colour classes
      map to ``"bold"`` since Telegraph rejects colour attributes; which
      classes count is the ONE genuine per-site hook here.
    * Sibling runs with the same ``href``/``formats`` are NOT merged —
      they stay separate (downstream renderer picks first occurrence per
      run, so adjacent identical runs are harmless).
    * Past ``MAX_RUNS_PER_BLOCK`` / ``MAX_TEXT_FOR_RUNS`` the runs are
      dropped and the block's full text is returned as a single plain run
      (Decision 8).
    """
    runs: List[Dict] = []
    buf: List[str] = []
    fmt_stack: List[str] = []

    def current_formats():
        # Preserve order of accumulation, dedup while keeping first-seen order.
        seen: List[str] = []
        for f in fmt_stack:
            if f not in seen:
                seen.append(f)
        return seen

    def flush(href=None):
        if not buf:
            return
        combined = "".join(buf)
        if combined:
            run: Dict = {"text": combined}
            if href:
                run["href"] = href
            fmts = current_formats()
            if fmts:
                run["formats"] = list(fmts)
            runs.append(run)
        buf.clear()

    def walk(element):
        for child in element.children:
            if isinstance(child, str):
                buf.append(str(child))
                continue
            name = getattr(child, "name", None)
            if name == "a":
                href = safe_href(child.get("href"))
                # Recurse into anchor children to capture nested <strong>/etc.
                # and emit a single run per anchor; if an inner format applies,
                # we attach it to the run alongside the href.
                inner_buf: List[str] = []
                inner_fmts: List[str] = []

                def collect(el):
                    inner_name = getattr(el, "name", None)
                    if isinstance(el, str):
                        inner_buf.append(str(el))
                        return
                    fmt = _INLINE_FORMAT_TAGS.get(inner_name)
                    color_fmt = "bold" if has_color_class(el) else None
                    pushed = []
                    if fmt:
                        inner_fmts.append(fmt)
                        pushed.append(fmt)
                    if color_fmt and color_fmt not in inner_fmts:
                        inner_fmts.append(color_fmt)
                        pushed.append(color_fmt)
                    for sub in getattr(el, "children", []):
                        collect(sub)
                    for _ in pushed:
                        inner_fmts.pop()

                for sub in child.children:
                    collect(sub)
                link_text = "".join(inner_buf).strip()
                # Re-derive a deduped format list (collect could push the same
                # format twice across siblings — keep first occurrence order).
                seen_inner: List[str] = []
                for f in current_formats():
                    seen_inner.append(f)
                # Note: anchor inner formats only matter if anchor itself or
                # ancestors are formatted. We use the OUTER fmt_stack to attach
                # ambient formatting to anchors (e.g., paragraph-wide <strong>).
                if href and link_text:
                    flush()
                    run: Dict = {"text": link_text, "href": href}
                    fmts = current_formats()
                    if fmts:
                        run["formats"] = list(fmts)
                    runs.append(run)
                elif link_text:
                    # Drop unsafe href, keep plain text inline.
                    buf.append(link_text)
                continue
            # Inline-format tag handling: push format marker(s) onto stack,
            # walk children, pop. Color class is treated as "bold" emphasis.
            fmt = _INLINE_FORMAT_TAGS.get(name)
            color_fmt = "bold" if has_color_class(child) else None
            pushed: List[str] = []
            # Decide whether a format actually OPENS here before touching the
            # stack. A color class whose format is already on the stack pushes
            # nothing, and must not trigger the pre-flush below (it would split
            # one run into two adjacent runs carrying identical formats).
            opens_format = bool(fmt) or bool(
                color_fmt and color_fmt not in fmt_stack
            )
            if opens_format:
                # Flush the pending plain-text buffer BEFORE the new format goes
                # onto the stack — `flush()` stamps the CURRENT stack onto the
                # buffered text, so flushing afterwards back-dates this format
                # onto the prefix that precedes the span.
                # Fixed 2026-07-28: the flush used to sit inside `if pushed:`,
                # below the pushes, so `<p>Plain <strong>bold</strong> tail.</p>`
                # published as `<strong>Plain </strong><strong>bold</strong>` —
                # everything before the first bold span in a paragraph came out
                # bold. The comment already described the correct order; only
                # the code disagreed.
                flush()
            if fmt:
                fmt_stack.append(fmt)
                pushed.append(fmt)
            if color_fmt and color_fmt not in fmt_stack:
                fmt_stack.append(color_fmt)
                pushed.append(color_fmt)
            if pushed:
                walk(child)
                flush()
                for _ in pushed:
                    fmt_stack.pop()
            else:
                walk(child)

    walk(tag)
    flush()
    # Normalize whitespace inside each run; trim leading/trailing on edges.
    for r in runs:
        r["text"] = re.sub(r"\s+", " ", r["text"])
    if runs:
        runs[0]["text"] = runs[0]["text"].lstrip()
        runs[-1]["text"] = runs[-1]["text"].rstrip()
    runs = [r for r in runs if r["text"]]

    # Resource bounds (Decision 8). Checked AFTER the walk because the walk
    # itself is linear in the DOM; what has to be bounded is the runs list
    # handed downstream, where locating each run costs a scan of the text.
    if runs and len(runs) > MAX_RUNS_PER_BLOCK:
        return [{"text": text_from_runs(runs)}]
    if runs:
        flat = text_from_runs(runs)
        if len(flat) > MAX_TEXT_FOR_RUNS:
            return [{"text": flat}]
    return runs


def looks_like_heading(text: str, runs: List[Dict]) -> bool:
    """True when a paragraph reads as a section title (Decision 2).

    The rule: the text is covered ENTIRELY by one merged bold span, and it
    does not end in a full stop. Sources that author section titles as a
    whole-bold paragraph (t-hunted) then get real headings instead of bold
    prose.

    THERE IS NO LENGTH LIMIT. That is a deliberate deviation from the
    user-spec, approved by the operator: real section titles on these sites
    run long, and a cap silently reclassified them. A test pins the absence
    so that a limit reintroduced later fails loudly.

    Ellipsis endings (``…`` and ``….``) count as heading-compatible along
    with ``?`` and ``!``; only a plain terminal ``.`` disqualifies. The check
    is ``str.endswith``, never a regex — Decision 8's ReDoS contract.

    Coverage is computed by locating each bold run with ``text.find`` and
    merging adjacent/overlapping spans, then requiring exactly one merged
    span with nothing left over on either side. The ``find`` mechanic is why
    a repeated word matters: ``<p><strong>Ford</strong> vs Ford</p>`` must
    NOT read as covered.
    """
    if not text or not runs:
        return False
    if text.endswith("…") or text.endswith("…."):
        pass  # heading-compatible ellipsis
    elif text.endswith("."):
        return False

    spans = []
    for run in runs:
        if "bold" not in (run.get("formats") or []):
            continue
        needle = (run.get("text") or "").strip()
        if not needle:
            continue
        pos = text.find(needle)
        if pos < 0:
            return False
        spans.append((pos, pos + len(needle)))
    if not spans:
        return False

    spans.sort()
    merged_start, merged_end = spans[0]
    for start, end in spans[1:]:
        if start > merged_end:
            return False  # a gap → more than one span → prose, not a title
        merged_end = max(merged_end, end)
    return not text[:merged_start].strip() and not text[merged_end:].strip()


# --------------------------------------------------------------------------- #
# BlockBuilder                                                                #
# --------------------------------------------------------------------------- #


def _default_img_src(img) -> Optional[str]:
    """Conservative default: the plain ``src`` attribute, scheme-checked."""
    return safe_img_src(img.get("src"))


def _default_dedup_key(src: str) -> str:
    """Strip the query string — the WordPress ``?w=600`` size variants of one
    upload collapse, distinct paths stay distinct."""
    return src.split("?", 1)[0]


def _no_chrome_class(node) -> bool:
    """Conservative default for the junk-class seam: nothing is chrome."""
    return False


class BlockBuilder:
    """Walks a parsed DOM and accumulates canonical blocks.

    OWNS ITS STATE. ``blocks`` and ``seen_image_bases`` used to be captured by
    the enclosing parse function's frame; here they are instance fields. ONE
    BUILDER PER ARTICLE — a module-level singleton would leak blocks between
    articles and dedup one article's images against another's.

    The four per-site seams are the constructor's keyword arguments; every
    default is conservative. ``has_color_class`` is the ``runs_from_tag`` hook
    threaded through, not a fifth site knob.
    """

    #: Tags the walker emits itself. ``ul`` / ``ol`` are deliberately ABSENT
    #: so the wrapper fallback recurses into them and reaches their ``<li>``
    #: children, which have their own branch. ``"li"`` is listed as
    #: documentation: it pins that ``<li>`` is handled by that branch and does
    #: not fall through to generic recursion.
    HANDLED_TAGS = frozenset({
        "p", "h1", "h2", "h3", "h4", "h5", "h6",
        "figure", "img", "iframe",
        "li",
    })

    def __init__(
        self,
        *,
        has_color_class=_no_color_class,
        is_chrome_class=_no_chrome_class,
        pick_img_src=_default_img_src,
        image_dedup_key=_default_dedup_key,
        video_hosts=(),
        video_provider="",
        headings_from_bold: bool = False,
    ):
        self.blocks: List[Dict] = []
        self.seen_image_bases = set()
        self._has_color_class = has_color_class
        self._is_chrome_class = is_chrome_class
        self._pick_img_src = pick_img_src
        self._image_dedup_key = image_dedup_key
        self._video_hosts = tuple(video_hosts)
        self._video_provider = video_provider
        self._headings_from_bold = headings_from_bold

    # -- runs ------------------------------------------------------------- #

    def _runs(self, tag) -> List[Dict]:
        return runs_from_tag(tag, has_color_class=self._has_color_class)

    # -- emitters --------------------------------------------------------- #

    def emit_paragraph(self, p_tag):
        """Emit paragraph block(s) for one ``<p>``.

        A single ``<p>`` containing ``<br>`` is SPLIT into one block per
        segment. WordPress editors author series/case checklists that way
        (``<p>#151 – <strong>Alphard</strong><br>#152 – …</p>``), and
        BeautifulSoup's ``get_text`` collapses ``<br>`` into a space, so the
        whole list otherwise lands in one block and the LLM splice cannot
        un-collapse it. Verified live 2026-05-14 on the mix-3-H case report,
        where the rendered page had five items on one line.
        """
        if not p_tag.find("br"):
            runs = self._runs(p_tag)
            if not runs:
                return
            text = text_from_runs(runs)
            if not text:
                return
            self._append_text_block(text, runs)
            return

        segments: List[List] = [[]]
        for child in p_tag.children:
            if getattr(child, "name", None) == "br":
                segments.append([])
            else:
                segments[-1].append(child)

        for seg_children in segments:
            if not seg_children:
                continue
            seg_html = "".join(str(c) for c in seg_children)
            wrapper = _parse_fragment(seg_html)
            if wrapper is None:
                continue
            runs = self._runs(wrapper)
            if not runs:
                continue
            text = text_from_runs(runs)
            if not text:
                continue
            self._append_text_block(text, runs)

    def _append_text_block(self, text: str, runs: List[Dict]):
        """Paragraph, unless the opt-in heading heuristic claims it."""
        if self._headings_from_bold and looks_like_heading(text, runs):
            self.blocks.append(
                {"type": "heading", "level": 3, "text": text, "runs": runs}
            )
            return
        self.blocks.append({"type": "paragraph", "text": text, "runs": runs})

    def emit_heading(self, h_tag, level: int):
        """Level-aware dispatch (Decisions 2 + 3 of orangetrack-rendering-fixes).

        h2 / h3 / h4 → ``heading`` normalised to ``level=3``: these sites use
        them as full section headers and typically have one section level.
        h5 → ``paragraph``, preserving the ``babc67c`` carve-out
        (SESSION-2026-05-06 break 3): ``<h5>`` is used as an in-paragraph
        marker with long descriptive text and heading typography looked
        uneven. h1 / h6 never reach here — the walk drops them.
        """
        runs = self._runs(h_tag)
        if not runs:
            return
        text = text_from_runs(runs)
        if not text:
            return
        if level in (2, 3, 4):
            self.blocks.append(
                {"type": "heading", "level": 3, "text": text, "runs": runs}
            )
            return
        # level == 5 (and any other unexpected level): paragraph.
        self.blocks.append({"type": "paragraph", "text": text, "runs": runs})

    def emit_image(self, img_tag, caption: str = ""):
        src = self._pick_img_src(img_tag)
        if not src:
            return
        base = self._image_dedup_key(src)
        if base in self.seen_image_bases:
            return
        self.seen_image_bases.add(base)
        block: Dict = {"type": "image", "src": src}
        if caption:
            block["caption"] = caption
        self.blocks.append(block)

    def emit_iframe(self, iframe_tag):
        raw_src = iframe_tag.get("src") or ""
        embed = video_embed_url(
            raw_src, hosts=self._video_hosts, provider=self._video_provider
        )
        if not embed:
            return
        self.blocks.append({"type": "video", "src": embed})

    def emit_list_item(self, li_tag):
        """``<li>`` → a ``list_item`` block WITHOUT a bullet.

        The bullet is prepended in ``telegraph_publisher`` after LLM
        translation, so it survives any stripping or translation (AC2).
        ``<ul>`` and ``<ol>`` are identical here. Empty / whitespace-only
        ``<li>`` emits nothing.
        """
        runs = self._runs(li_tag)
        if not runs:
            return
        text = text_from_runs(runs)
        if not text:
            return
        self.blocks.append({"type": "list_item", "text": text, "runs": runs})

    # -- walk -------------------------------------------------------------- #

    def walk(self, node):
        """Walk direct children and recurse selectively.

        BS4's ``descendants`` yields ALL nodes in DOM order, which would
        double-count a ``<p>`` nested under a ``<figure>``. Instead known
        content tags are emitted once and wrapper tags are recursed into.
        Dispatch is by tag NAME — no Gutenberg classes (Decision 3).
        """
        for child in list(node.children):
            name = getattr(child, "name", None)
            if not name:
                continue  # NavigableString — skip; <p> walker handles text.
            if self._is_chrome_class(child):
                continue  # site chrome (share buttons, related posts) — drop.
            if name == "p":
                # A paragraph wrapping ONLY an iframe / img: media takes
                # precedence, otherwise BS4's get_text on the iframe yields a
                # run with empty text.
                inner_iframes = child.find_all("iframe")
                inner_imgs = child.find_all("img")
                if inner_iframes and not child.get_text(strip=True):
                    for iframe in inner_iframes:
                        self.emit_iframe(iframe)
                    continue
                # Mixed paragraph: emit text first, then nested media.
                self.emit_paragraph(child)
                for iframe in inner_iframes:
                    self.emit_iframe(iframe)
                for img in inner_imgs:
                    if img.find_parent("figure"):
                        # Will be picked up by the figure walker.
                        continue
                    self.emit_image(img)
                continue
            if name in ("h2", "h3", "h4"):
                self.emit_heading(child, int(name[1]))
                continue
            if name == "h5":
                self.emit_heading(child, 5)
                continue
            if name in ("h1", "h6"):
                # h1 is consumed as the article title; h6 is decorative.
                continue
            if name == "figure":
                img = child.find("img")
                if img:
                    cap_tag = child.find("figcaption")
                    caption = cap_tag.get_text(" ", strip=True) if cap_tag else ""
                    self.emit_image(img, caption=caption)
                # Video embed wrapped in <figure> (typical WP output:
                # <figure class="wp-block-embed"><div><iframe>...). The figure
                # handler runs ONCE per figure, so pick up iframes nested
                # anywhere inside it — figure is not a wrapper tag here.
                for inner_iframe in child.find_all("iframe"):
                    self.emit_iframe(inner_iframe)
                # Nested figures (carousel / gallery) — recurse for more <img>.
                for nested in child.find_all("figure"):
                    if nested is child:
                        continue
                    nested_img = nested.find("img")
                    if not nested_img:
                        continue
                    cap = nested.find("figcaption")
                    cap_text = cap.get_text(" ", strip=True) if cap else ""
                    self.emit_image(nested_img, caption=cap_text)
                continue
            if name == "img":
                self.emit_image(child)
                continue
            if name == "iframe":
                self.emit_iframe(child)
                continue
            if name == "li":
                self.emit_list_item(child)
                continue
            # Wrapper tag (div / section / article / ul / ol / etc.):
            # recurse so the inner <p>/<figure>/<iframe>/<li> get walked.
            if name not in self.HANDLED_TAGS:
                self.walk(child)


def _parse_fragment(html: str):
    """Wrap a ``<br>``-split segment back into a ``<p>`` so it can be walked.

    Imported lazily so the module's import graph stays honest about bs4 being
    the only third-party dependency.
    """
    from bs4 import BeautifulSoup

    return BeautifulSoup(f"<p>{html}</p>", "html.parser").p
