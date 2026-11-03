"""Real, local transports exercise resource limits and redirect boundaries."""

import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time

import pytest
import requests
from curl_cffi import requests as curl_requests

from http_fetch import FetchTooLarge, bounded_get


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def do_GET(self):
        self.server.seen.append(self.path)
        try:
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/target")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.path in ("/trickle", "/chunked", "/stall", "/gzip-trickle"):
                self.send_response(200)
                if self.path == "/chunked":
                    self.send_header("Transfer-Encoding", "chunked")
                if self.path == "/gzip-trickle":
                    self.send_header("Content-Encoding", "gzip")
                self.send_header("Connection", "close")
                self.end_headers()
                if self.path == "/stall":
                    time.sleep(0.4)
                if self.path == "/gzip-trickle":
                    self.wfile.write(b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x03")
                for _ in range(100):
                    chunk = b"1\r\nx\r\n" if self.path == "/chunked" else b"x"
                    if self.path == "/gzip-trickle":
                        chunk = b"\x00\x00\x00\xff\xff"
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    time.sleep(0.01)
                return
            body = b"<html>bounded body</html>"
            if self.path == "/oversize":
                body = b"x" * 4096
            if self.path == "/gzip":
                body = gzip.compress(b"x" * 300_000)
            if self.path == "/target":
                body = (b'<h1>Hot Wheels</h1><h3 class="post-title">Hot Wheels</h3>'
                        b'<div class="post-body entry-content"><p>News body.</p></div>')
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            if self.path == "/gzip":
                self.send_header("Content-Encoding", "gzip")
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


@pytest.fixture
def server(monkeypatch):
    # Local fixture traffic only; production proxy/TLS settings are untouched.
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    instance = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    instance.daemon_threads = True
    instance.seen = []
    thread = threading.Thread(
        target=lambda: instance.serve_forever(poll_interval=0.01), daemon=True,
    )
    thread.start()
    yield instance, f"http://127.0.0.1:{instance.server_port}"
    instance.shutdown()
    instance.server_close()
    thread.join(timeout=1)


@pytest.fixture(params=[False, True], ids=["requests", "curl_cffi"])
def transport(request):
    curl = request.param
    session = curl_requests.Session() if curl else requests.Session()
    try:
        yield session, curl
    finally:
        session.close()


def test_real_response_buffer_and_close(server, transport):
    _instance, url = server
    session, curl = transport
    result = bounded_get(session, url + "/ok", max_bytes=128, timeout=1, curl=curl)
    assert result.content == b"<html>bounded body</html>"
    assert result.text == "<html>bounded body</html>"
    if not curl:
        assert result.raw.closed


def test_redirect_is_not_followed(server, transport):
    instance, url = server
    session, curl = transport
    with pytest.raises(requests.RequestException, match="redirect rejected"):
        bounded_get(session, url + "/redirect", max_bytes=128, timeout=1, curl=curl)
    assert instance.seen == ["/redirect"]


def test_oversize_is_stopped_before_accumulation(server, transport):
    _instance, url = server
    session, curl = transport
    with pytest.raises(FetchTooLarge):
        bounded_get(session, url + "/oversize", max_bytes=128, timeout=1, curl=curl)


def test_chunked_body_without_content_length_hits_byte_cap(server, transport):
    _instance, url = server
    session, curl = transport
    with pytest.raises(FetchTooLarge):
        bounded_get(session, url + "/chunked", max_bytes=8, timeout=1, curl=curl)


def test_compressed_body_is_bounded_or_rejected_before_decode(server, transport):
    _instance, url = server
    session, curl = transport
    expected = FetchTooLarge if curl else requests.RequestException
    with pytest.raises(expected):
        bounded_get(session, url + "/gzip", max_bytes=128, timeout=1, curl=curl)


@pytest.mark.parametrize("path", ["/trickle", "/chunked", "/stall", "/gzip-trickle"])
def test_real_continuous_and_stalled_bodies_have_bounded_lifetime(
    server, transport, path,
):
    _instance, url = server
    session, curl = transport
    started = time.monotonic()
    errors = (requests.RequestException, curl_requests.exceptions.RequestException)
    with pytest.raises(errors):
        bounded_get(session, url + path, max_bytes=4096, timeout=0.12, curl=curl)
    assert time.monotonic() - started < 0.8


@pytest.mark.parametrize("source", ["t_hunted", "lamley"])
def test_source_rejects_redirect_before_parsing_target(server, monkeypatch, source):
    instance, url = server
    import lamley_source
    import t_hunted_source

    if source == "t_hunted":
        monkeypatch.setattr(t_hunted_source, "_ALLOWED_HOSTS", ("127.0.0.1",))
        fetch = t_hunted_source.fetch_t_hunted_article
    else:
        monkeypatch.setattr(lamley_source, "_ALLOWED_HOSTS", ("127.0.0.1",))
        monkeypatch.setattr(lamley_source, "_last_request_time", 0.0)
        monkeypatch.setattr(lamley_source, "_cooldown_until", 0.0)
        fetch = lamley_source.fetch_lamley_article
    with requests.Session() as session:
        assert fetch(url + "/redirect", session=session) is None
    assert instance.seen == ["/redirect"]
