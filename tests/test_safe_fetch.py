import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from rainout_agent import safe_fetch
from rainout_agent.safe_fetch import FetchError, UnsafeURLError, is_public_ip, resolve_public_ip, safe_get, validate_url


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/cal.ics",
        "gopher://example.com/",
        "http://127.0.0.1/cal.ics",
        "http://10.1.2.3/",
        "http://172.16.0.1/",
        "http://192.168.1.10/",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.64.0.1/",
        "http://0.0.0.0/",
        "http://[::1]/",
        "http://[fd00::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://localhost/",
        "http://foo.localhost/",
        "http://metadata.google.internal/",
        "http://printer.local/",
        "https://example.com:8443/cal.ics",
        "http://user:pass@example.com/cal.ics",
        "http:///nohost",
    ],
)
def test_validate_url_blocks_unsafe_targets(url):
    with pytest.raises(UnsafeURLError):
        validate_url(url)


def test_validate_url_allows_public_http_https_and_webcal():
    assert validate_url("https://example.com/cal.ics")[1:3] == ("example.com", 443)
    assert validate_url("http://example.com/cal.ics")[2] == 80
    scheme, host, port, url = validate_url("webcal://calendar.example.org/team.ics")
    assert (scheme, port, url) == ("https", 443, "https://calendar.example.org/team.ics")
    assert validate_url("http://8.8.8.8/")[1] == "8.8.8.8"


def test_is_public_ip():
    assert is_public_ip("8.8.8.8") and is_public_ip("2606:4700:4700::1111")
    for ip in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "100.64.1.1", "198.18.0.1", "224.0.0.1", "::1", "fe80::1", "::ffff:10.0.0.1", "nonsense"):
        assert not is_public_ip(ip), ip


def test_dns_answers_pointing_inside_are_blocked(monkeypatch):
    def fake_getaddrinfo(host, port, type=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(UnsafeURLError):
        resolve_public_ip("rebind.example", 443)


def test_public_dns_answer_is_returned(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda h, p, type=0: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", p))])
    assert resolve_public_ip("example.com", 443) == "93.184.216.34"


# A local server stands in for a public host; the resolver is pinned to it.
class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/cal.ics":
            body = b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/calendar")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/redirect-ok":
            self.send_response(302)
            self.send_header("Location", "/cal.ics")
            self.end_headers()
        elif self.path == "/redirect-private":
            self.send_response(302)
            self.send_header("Location", "http://10.0.0.1/cal.ics")
            self.end_headers()
        elif self.path == "/redirect-loop":
            self.send_response(302)
            self.send_header("Location", "/redirect-loop")
            self.end_headers()
        elif self.path == "/big":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"x" * 5000)
        elif self.path == "/big-declared":
            self.send_response(200)
            self.send_header("Content-Length", "999999999")
            self.end_headers()
        elif self.path == "/slow":
            self.send_response(200)
            self.end_headers()
            for _ in range(20):
                self.wfile.write(b"x")
                self.wfile.flush()
                time.sleep(0.1)
        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture(scope="module")
def server(request):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv.server_address[1]
    srv.shutdown()


@pytest.fixture
def pinned(server, monkeypatch):
    # Allow the test port and pin every hostname to the local test server.
    monkeypatch.setattr(safe_fetch, "ALLOWED_PORTS", {80, 443, server})
    return lambda host, port: "127.0.0.1"


def test_safe_get_fetches_and_follows_safe_redirects(server, pinned):
    result = safe_get(f"http://calendar.example:{server}/redirect-ok", resolver=pinned)
    assert result.status == 200 and result.body.startswith(b"BEGIN:VCALENDAR")
    assert result.url.endswith("/cal.ics")


def test_safe_get_revalidates_redirect_targets(server, pinned):
    with pytest.raises(UnsafeURLError):
        safe_get(f"http://calendar.example:{server}/redirect-private", resolver=pinned)
    with pytest.raises(FetchError):
        safe_get(f"http://calendar.example:{server}/redirect-loop", resolver=pinned)


def test_safe_get_enforces_size_time_and_status(server, pinned):
    with pytest.raises(FetchError, match="larger"):
        safe_get(f"http://calendar.example:{server}/big", max_bytes=1000, resolver=pinned)
    with pytest.raises(FetchError, match="larger"):
        safe_get(f"http://calendar.example:{server}/big-declared", resolver=pinned)
    started = time.monotonic()
    with pytest.raises(FetchError):
        safe_get(f"http://calendar.example:{server}/slow", total_timeout=0.5, resolver=pinned)
    assert time.monotonic() - started < 1.5
    with pytest.raises(FetchError, match="404"):
        safe_get(f"http://calendar.example:{server}/missing", resolver=pinned)
