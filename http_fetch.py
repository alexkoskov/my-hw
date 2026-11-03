"""Bound article downloads without following unvalidated redirects.

curl_cffi uses a bounded callback rather than its unbounded streaming queue,
retaining libcurl's total request timeout. Requests uses raw ``read1`` calls,
a short inactivity timeout and a body deadline. The latter is not a strict
whole-HTTP deadline for trickled headers or chunk framing. It requests identity
encoding and refuses unexpected compression before decoding it.
"""

from __future__ import annotations

import time

import requests
from urllib3.response import HTTPResponse


_READ_SIZE = 8192
_READ_IDLE_SECONDS = 2.0
_CURL_WRITEFUNC_ERROR = 0xFFFFFFFF


class FetchTooLarge(requests.RequestException):
    """The wire or decoded response exceeded the configured byte cap."""

    def __init__(self, size: int, limit: int):
        self.size = size
        super().__init__(f"response too large: {size} bytes (limit {limit})")


def _check_status(response) -> None:
    if 300 <= response.status_code < 400:
        raise requests.RequestException("article redirect rejected")


def _buffered_content(response, limit: int) -> bytes:
    """Support already-buffered/injected responses under the same size cap."""
    content = getattr(response, "content", None)
    if not isinstance(content, bytes):
        text = getattr(response, "text", None)
        if not isinstance(text, str):
            raise requests.RequestException("response has no binary body")
        content = text.encode("utf-8")
    if len(content) > limit:
        raise FetchTooLarge(len(content), limit)
    return content


def bounded_get(http, url: str, *, max_bytes: int, timeout: float,
                curl: bool = False, **kwargs):
    """Return a closed, buffered response; cap body bytes before accumulation.

    The caller still owns HTTP-status policy (including source-specific 429
    retries). Redirects are always disabled and rejected; a new allowed URL must
    pass the source's own allowlist in a separate call.
    """
    if max_bytes <= 0 or timeout <= 0:
        raise ValueError("positive byte limit and timeout required")
    kwargs["allow_redirects"] = False
    response = None
    deadline = time.monotonic() + timeout
    try:
        if curl:
            body = bytearray()
            failure = None

            def receive(chunk):
                nonlocal failure
                size = len(body) + len(chunk)
                if size > max_bytes:
                    failure = FetchTooLarge(size, max_bytes)
                    return _CURL_WRITEFUNC_ERROR
                body.extend(chunk)
                return len(chunk)

            try:
                response = http.get(url, timeout=timeout, stream=False,
                                    content_callback=receive, **kwargs)
            except Exception as exc:
                if failure is not None:
                    raise failure from exc
                raise
            _check_status(response)
            # An injected/already-buffered transport need not call receive.
            response.content = bytes(body) if body else _buffered_content(
                response, max_bytes,
            )
            return response

        headers = dict(kwargs.pop("headers", {}) or {})
        headers = {k: v for k, v in headers.items()
                   if k.lower() != "accept-encoding"}
        headers["Accept-Encoding"] = "identity"
        response = http.get(
            url, timeout=(timeout, min(timeout, _READ_IDLE_SECONDS)),
            stream=True, headers=headers, **kwargs,
        )
        _check_status(response)
        raw = getattr(response, "raw", None)
        if not isinstance(raw, HTTPResponse):
            _buffered_content(response, max_bytes)
            return response
        encoding = response.headers.get("Content-Encoding", "").strip().lower()
        if encoding not in ("", "identity"):
            raise requests.RequestException("unexpected compressed response")
        length = response.headers.get("Content-Length", "")
        if str(length).isdigit() and int(length) > max_bytes:
            raise FetchTooLarge(int(length), max_bytes)
        body = bytearray()
        while True:
            if time.monotonic() >= deadline:
                raise requests.Timeout("article body deadline exceeded")
            try:
                chunk = raw.read1(min(_READ_SIZE, max_bytes - len(body) + 1),
                                  decode_content=False)
            except Exception as exc:
                raise requests.RequestException("article body read failed") from exc
            if time.monotonic() >= deadline:
                raise requests.Timeout("article body deadline exceeded")
            if not chunk:
                break
            size = len(body) + len(chunk)
            if size > max_bytes:
                raise FetchTooLarge(size, max_bytes)
            body.extend(chunk)
        response._content = bytes(body)
        response._content_consumed = True
        return response
    finally:
        if response is not None:
            close = getattr(response, "close", None)
            if callable(close):
                close()
