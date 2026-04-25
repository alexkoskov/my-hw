"""Synthetic HTML builder for Mattel RSC-flight payloads.

Used by ``tests/test_mattel_news_source.py`` and
``tests/test_mattel_integration.py`` to produce deterministic HTML pages whose
shape matches the live ``self.__next_f.push([1, "..."])`` streaming-payload
format Mattel's Next.js App Router emits (verified live 2026-04-24).

Public API:
- ``_make_flight_listing(entries)`` — wraps a list of entry dicts into a
  single push that contains the ``article2.entries`` anchor.
- ``_make_flight_article(entry, body_html=None, body_chunks=1, truncate=False)``
  — wraps one entry's article page; if ``body_html`` is given, splits the
  text-row across ``body_chunks`` separate pushes (AC8 multi-chunk coverage)
  and optionally truncates the streamed content below the advertised
  hex-length (AC9 truncated-body coverage).

All defaults use placeholder URLs (``example.com``, ``placeholder.invalid``)
so a stray test cannot accidentally surrogate a real Mattel/Contentstack URL.
"""

from __future__ import annotations

import json
from typing import List, Optional


_COMPACT = (",", ":")  # match Next.js output: no spaces after , or :


def _to_js_string_literal_body(s: str) -> str:
    """Encode ``s`` as the inner body of a JS double-quoted string literal.

    ``json.dumps`` produces a fully-quoted JSON string (which is also a valid
    JS string literal); we strip the surrounding quotes to get just the body
    suitable for embedding inside ``self.__next_f.push([1, "<body>"])``.
    """
    return json.dumps(s, ensure_ascii=False)[1:-1]


def _compact_json(obj) -> str:
    """Dump ``obj`` to compact JSON (no spaces) — matches the live Next.js
    output that the parser anchors on (e.g., ``"article2":{"entries":[``).
    """
    return json.dumps(obj, ensure_ascii=False, separators=_COMPACT)


def _push(content: str) -> str:
    """Wrap ``content`` (the inner row text) into a script-tag push call."""
    body = _to_js_string_literal_body(content)
    return f'<script>self.__next_f.push([1,"{body}"])</script>'


def _make_flight_listing(entries: List[dict]) -> str:
    """Build a synthetic listing HTML containing one push with all entries.

    The push is row 6 (matching the live 2026-04-24 layout). Its content is
    a JSON envelope wrapping ``article2.entries`` — the parser only anchors
    on the substring ``"article2":{"entries":[`` so the surrounding object is
    filler. Each entry dict is encoded via ``json.dumps`` so callers can pass
    plain Python data and let the builder do all escaping.
    """
    envelope = {
        "props": {
            "pageProps": {
                "page": {
                    "data": {
                        "state": {
                            "article2": {
                                "entries": entries,
                            },
                        },
                    },
                },
            },
        },
    }
    row_content = "6:" + _compact_json(envelope)
    return (
        "<html><body>"
        '<script>self.__next_f=self.__next_f||[]</script>'
        + _push(row_content)
        + "</body></html>"
    )


def _make_flight_article(
    entry: dict,
    body_html: Optional[str] = None,
    body_chunks: int = 1,
    truncate: bool = False,
) -> str:
    """Build a synthetic article-page HTML.

    The article's listing-shaped entry sits inside ``article2.entries``
    (live article pages contain the same listing block as the listing page).
    If ``body_html`` is provided, the entry's ``body`` field is set to
    ``"$53"`` and a separate text-row is emitted as ``53:T<hex_len>,<content>``.

    Parameters
    ----------
    entry: dict
        The entry to encode (must contain ``handle``, ``title``, ``date``).
        The ``body`` field is overwritten when ``body_html`` is provided.
    body_html: str | None
        Raw body HTML to encode into the text-row. None = no body field.
    body_chunks: int
        Number of pushes to split the body content across (>=1). Used for
        AC8 (boundary-spanning resolution).
    truncate: bool
        If True, advertise a hex length that exceeds the actually streamed
        content (simulates an unresolvable reference per AC9).
    """
    entry = dict(entry)  # don't mutate caller's dict

    pushes: List[str] = []
    pushes.append('<script>self.__next_f=self.__next_f||[]</script>')

    if body_html is not None:
        row_id = "53"
        entry["body"] = "$" + row_id

        if truncate:
            advertised_len = len(body_html) + 64  # advertise more than streamed
        else:
            advertised_len = len(body_html)
        hex_len = format(advertised_len, "x")

        envelope = {
            "props": {
                "pageProps": {
                    "page": {
                        "data": {
                            "state": {
                                "article2": {"entries": [entry]},
                            },
                        },
                    },
                },
            },
        }
        envelope_row = "6:" + _compact_json(envelope)
        pushes.append(_push(envelope_row))

        chunks = max(1, body_chunks)
        if chunks == 1:
            pushes.append(_push(f"{row_id}:T{hex_len}," + body_html))
        else:
            # Split body_html into N roughly-equal slices. The first push
            # carries the row-marker prefix + first slice; subsequent pushes
            # carry continuation text only (no marker).
            slice_len = max(1, len(body_html) // chunks)
            slices: List[str] = []
            pos = 0
            for i in range(chunks - 1):
                slices.append(body_html[pos:pos + slice_len])
                pos += slice_len
            slices.append(body_html[pos:])
            pushes.append(_push(f"{row_id}:T{hex_len}," + slices[0]))
            for piece in slices[1:]:
                pushes.append(_push(piece))
    else:
        # No body: emit envelope only.
        envelope = {
            "props": {
                "pageProps": {
                    "page": {
                        "data": {
                            "state": {
                                "article2": {"entries": [entry]},
                            },
                        },
                    },
                },
            },
        }
        envelope_row = "6:" + _compact_json(envelope)
        pushes.append(_push(envelope_row))

    return "<html><body>" + "".join(pushes) + "</body></html>"
