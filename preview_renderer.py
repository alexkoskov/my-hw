#!/usr/bin/env python3
"""Render a Telegra.ph node tree into a standalone HTML document for local
browser preview.

This module exists to support `hw_review preview N`, which writes the rendered
HTML to `~/.cache/hw-review/hw-{uuid4}.html` and opens it via `webbrowser.open`
under the `file://` origin. Because the browser trusts `file://` strongly, the
renderer hardens the output against XSS from a compromised upstream article
with three independent layers (tech-spec Decision 1):

  1. **Tag allowlist.** Only the tags that `telegraph_publisher._build_content*`
     is allowed to emit pass through (`p / figure / img / figcaption / iframe /
     h3 / h4 / hr / i / b / a`). Any other tag — including `<script>`,
     `<style>`, `<object>`, `<svg>` — is silently dropped together with its
     children. This mirrors the Telegra.ph validator's behaviour for unknown
     tags (see `work/completed/telegraph-pipeline/post-format.md`).

  2. **URL-scheme allowlist.** `img src`, `iframe src`, `a href` must match
     `^https?://` (IGNORECASE). Any other scheme or a relative path causes the
     attribute — and only that attribute — to be dropped. Blocks
     `javascript:`, `data:`, `file:`, `vbscript:` and `/path`.

  3. **CSP meta tag** `default-src 'none'; img-src https:; frame-src https:;
     style-src 'unsafe-inline'` in the rendered `<head>`. Defence-in-depth in
     case something slips past the allowlists. `style-src 'unsafe-inline'`
     specifically permits the small inline `<style>` block this module emits
     for readability; nothing else.

All text content (including Cyrillic titles and paragraphs) and all retained
attribute values pass through `html.escape(..., quote=True)` so `<`, `>`, `&`,
`"`, `'` cannot break out of their context. Cyrillic characters are not
touched — we emit valid UTF-8 directly.

The module is intentionally side-effect free: no disk I/O, no network, no
environment reads. Writing the rendered HTML to a cache file is the CLI's
responsibility (Task 7).

Public surface: `render_html(nodes, title) -> str`.
"""

import html
import re

__all__ = ["render_html"]


# Tags the Telegra.ph publisher is allowed to emit. Any other tag is dropped.
# Must stay in lock-step with `telegraph_publisher._build_content*`.
#
# `strong`/`u`/`s` added 2026-07-28. The publisher has emitted them since the
# orangetrack runs renderer shipped (`telegraph_publisher._FORMAT_TAGS` maps
# bold→strong, underline→u, strikethrough→s), but they were never mirrored
# here — and `_render_node` drops an unknown tag TOGETHER WITH ITS CHILDREN, so
# `<p>Hello <strong>bold</strong> world</p>` previewed as `<p>Hello  world</p>`
# with the word gone. Telegraph itself accepts the tag, so published pages were
# always fine; only the preview lied — which matters precisely because the
# preview is what a human checks the formatting with.
_ALLOWED_TAGS = frozenset({
    "p", "figure", "img", "figcaption", "iframe",
    "h3", "h4", "hr", "i", "b", "a",
    "strong", "u", "s",
})

# Tags without a closing form. `br` is listed for future proofing even though
# the current publisher does not emit it.
_VOID_TAGS = frozenset({"img", "hr", "br"})

# Per-tag URL attribute that must pass the scheme allowlist.
_URL_ATTR_TAGS = {"img": "src", "iframe": "src", "a": "href"}

# Only `http://` / `https://` schemes are allowed. The `^` anchor rejects
# leading whitespace (`  javascript:...`) and the re.IGNORECASE flag accepts
# `HTTPS://...` variants without letting `JavaScript:` through — `javascript`
# simply isn't in the allowlist.
_SAFE_URL_RE = re.compile(r"^https?://", re.IGNORECASE)

# Attribute names are restricted to simple ASCII identifiers to close the
# injection surface where a crafted key like `'x" onerror="alert(1)'` could
# break out of the tag — `html.escape` on the VALUE would not help if the
# KEY itself already contains `"`, `>` or whitespace. The Telegra.ph node
# tree only ever uses `src`, `href`, `alt`, `caption`, `level`, etc., so a
# conservative ASCII-letter pattern is sufficient and blocks everything else.
_SAFE_ATTR_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")

# Exact CSP meta tag — tests byte-compare against this string. Do NOT edit
# without updating `tests/test_preview_renderer.py::CSP_META_EXPECTED`.
_CSP_META = (
    '<meta http-equiv="Content-Security-Policy" '
    'content="default-src \'none\'; img-src https:; '
    'frame-src https:; style-src \'unsafe-inline\'">'
)

# Minimal readability styles. Keep dependency-free: no @font-face, no
# external URLs (CSP `default-src 'none'` would block them anyway and the
# preview runs from `file://` which is frequently offline).
_INLINE_STYLE = (
    "<style>"
    "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
    "max-width:720px;margin:2em auto;padding:0 1em;line-height:1.55;}"
    "figure{margin:1.5em 0;}"
    "img{max-width:100%;height:auto;display:block;}"
    "iframe{width:100%;aspect-ratio:16/9;border:0;}"
    "hr{border:0;border-top:1px solid #ccc;margin:1.5em 0;}"
    "</style>"
)


def _render_attrs(tag: str, attrs: dict) -> str:
    """Render a dict of attributes into a leading-space-prefixed string.

    For URL attributes on `img`/`iframe`/`a`, the scheme must match
    `_SAFE_URL_RE` or the attribute is dropped entirely (no escape, no keep).
    All other attribute values are HTML-escaped via `html.escape(.., quote=True)`.
    Non-string values are coerced via `str()` before escaping — defensive; the
    Telegra.ph node tree only ever contains strings.
    """
    if not attrs:
        return ""
    url_attr = _URL_ATTR_TAGS.get(tag)
    parts = []
    for key, value in attrs.items():
        if value is None:
            continue
        # Reject malformed attribute NAMES. A crafted key containing `"`,
        # `>`, whitespace or any non-identifier character could break out
        # of the tag even when the VALUE is properly escaped. The real
        # Telegra.ph tree only ever uses plain ASCII identifiers.
        if not isinstance(key, str) or not _SAFE_ATTR_NAME_RE.match(key):
            continue
        value_str = value if isinstance(value, str) else str(value)
        if key == url_attr:
            if not _SAFE_URL_RE.match(value_str):
                # Drop the attribute entirely — do NOT emit name="" either,
                # since an empty attribute can still collide in some parsers.
                continue
        parts.append(f' {key}="{html.escape(value_str, quote=True)}"')
    return "".join(parts)


def _render_node(node) -> str:
    """Render one node of the Telegra.ph tree.

    - Strings are HTML-escaped (leaf content like `"Источник: "`, subtitle,
      paragraph text — see `telegraph_publisher._footer_nodes`).
    - Dicts with a known `tag` render as `<tag attrs>inner</tag>` (or
      self-closing for void tags).
    - Dicts with an unknown tag → empty string. Their children are NOT
      rendered either: a compromised upstream could wrap a payload in
      `<script>…attack…</script>` and we must drop the payload too.
    - Anything else (numbers, None, lists) → empty string. Defensive: the
      real Telegra.ph tree never contains these.
    """
    if isinstance(node, str):
        return html.escape(node, quote=True)
    if not isinstance(node, dict):
        return ""
    tag = node.get("tag")
    if tag not in _ALLOWED_TAGS:
        return ""
    attr_str = _render_attrs(tag, node.get("attrs") or {})
    if tag in _VOID_TAGS:
        # Void tags carry no content — `children`, if any, is ignored by
        # design (matches HTML parser behaviour for `<img>`, `<hr>`, `<br>`).
        return f"<{tag}{attr_str}>"
    inner = "".join(_render_node(child) for child in (node.get("children") or []))
    return f"<{tag}{attr_str}>{inner}</{tag}>"


def render_html(nodes: list, title: str) -> str:
    """Render the Telegra.ph node tree into a standalone HTML document.

    Pure function: no side effects. The returned string is ready to write
    to a `.html` file and open via `webbrowser.open(file://...)`.

    Args:
        nodes: Telegra.ph node tree (the same shape emitted by
            `telegraph_publisher._build_content` / `_build_content_from_blocks`).
            Accepts any iterable of nodes; callers always pass a list.
        title: Article title; appears both in `<title>` and as an `<h1>`.
            HTML-special characters are escaped; Cyrillic is preserved.

    Returns:
        A complete HTML5 document starting with `<!DOCTYPE html>` and ending
        with `</html>`.
    """
    safe_title = html.escape(title, quote=True)
    body_parts = [_render_node(node) for node in (nodes or [])]
    return "".join([
        "<!DOCTYPE html>",
        "<html>",
        "<head>",
        '<meta charset="utf-8">',
        _CSP_META,
        f"<title>{safe_title}</title>",
        _INLINE_STYLE,
        "</head>",
        "<body>",
        f"<h1>{safe_title}</h1>",
        *body_parts,
        "</body>",
        "</html>",
    ])
