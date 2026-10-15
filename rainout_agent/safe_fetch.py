"""SSRF-hardened HTTP GET for user-supplied URLs (ICS calendars).

Rules:
* only ``http``/``https`` (``webcal://`` is treated as ``https://``);
* no credentials in the URL, only ports 80/443;
* every resolved address must be public (no private, loopback, link-local,
  multicast, reserved, CGNAT or cloud-metadata ranges);
* the connection goes to the vetted IP itself (Host header and TLS SNI keep the
  hostname), so DNS rebinding between check and connect is not possible;
* redirects are followed manually (at most 3), and each hop is re-validated;
* hard limits on response size and on total time.
"""
from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

MAX_BYTES = 1_000_000
TOTAL_TIMEOUT_S = 8.0
MAX_REDIRECTS = 3
ALLOWED_PORTS = {80, 443}
USER_AGENT = "Rainout Source schedule checker (+https://github.com/bizwaxx/Rain-Outsource)"
BLOCKED_HOSTNAMES = {"localhost", "localhost.localdomain", "metadata.google.internal", "metadata"}


class UnsafeURLError(ValueError):
    """The URL is not allowed (scheme, host, port or address)."""


class FetchError(RuntimeError):
    """The URL was allowed but could not be fetched within the limits."""


@dataclass
class FetchResult:
    url: str
    status: int
    content_type: str
    body: bytes


def _normalize_url(url: str) -> str:
    url = (url or "").strip()
    if url.lower().startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    return url


def is_public_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not (ip.is_multicast or ip.is_reserved or ip.is_loopback or ip.is_link_local or ip.is_private)


def validate_url(url: str) -> tuple[str, str, int, str]:
    """Check scheme/host/port. Returns (scheme, host, port, normalized_url)."""
    url = _normalize_url(url)
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise UnsafeURLError(f"invalid URL: {exc}") from None
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        raise UnsafeURLError("only http and https URLs are allowed")
    if parts.username or parts.password:
        raise UnsafeURLError("credentials in URLs are not allowed")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise UnsafeURLError("URL has no host")
    if host in BLOCKED_HOSTNAMES or host.endswith(".localhost") or host.endswith(".internal") or host.endswith(".local"):
        raise UnsafeURLError("host is not allowed")
    port = port or (443 if scheme == "https" else 80)
    if port not in ALLOWED_PORTS:
        raise UnsafeURLError("only ports 80 and 443 are allowed")
    try:
        ipaddress.ip_address(host)
        literal = True
    except ValueError:
        literal = False
    if literal and not is_public_ip(host):
        raise UnsafeURLError("private or reserved IP addresses are not allowed")
    return scheme, host, port, url


def resolve_public_ip(host: str, port: int) -> str:
    """Resolve ``host`` and require every address to be public. Returns one IP."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise FetchError(f"could not resolve host: {exc}") from None
    addresses = [info[4][0] for info in infos]
    if not addresses:
        raise FetchError("host has no addresses")
    if any(not is_public_ip(addr) for addr in addresses):
        raise UnsafeURLError("host resolves to a private or reserved address")
    ipv4 = [a for a in addresses if ":" not in a]
    return (ipv4 or addresses)[0]


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, ip, port, timeout):
        super().__init__(host, port, timeout=timeout)
        self._ip = ip

    def connect(self):
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, ip, port, timeout):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._ip = ip

    def connect(self):
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def safe_get(url: str, max_bytes: int = MAX_BYTES, total_timeout: float = TOTAL_TIMEOUT_S, resolver=resolve_public_ip) -> FetchResult:
    """GET a user-supplied URL under the SSRF rules above."""
    deadline = time.monotonic() + total_timeout
    current = url
    for _hop in range(MAX_REDIRECTS + 1):
        scheme, host, port, current = validate_url(current)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FetchError("timed out")
        ip = resolver(host, port)
        conn_cls = _PinnedHTTPSConnection if scheme == "https" else _PinnedHTTPConnection
        conn = conn_cls(host, ip, port, timeout=min(remaining, total_timeout))
        parts = urlsplit(current)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        try:
            conn.request("GET", path, headers={"User-Agent": USER_AGENT, "Accept": "text/calendar, text/plain, */*", "Accept-Encoding": "identity"})
            resp = conn.getresponse()
            if resp.status in {301, 302, 303, 307, 308}:
                location = resp.getheader("Location")
                conn.close()
                if not location:
                    raise FetchError("redirect without Location")
                current = urljoin(current, location)
                continue
            if resp.status != 200:
                raise FetchError(f"upstream returned HTTP {resp.status}")
            length = resp.getheader("Content-Length")
            if length and length.isdigit() and int(length) > max_bytes:
                raise FetchError(f"response larger than {max_bytes} bytes")
            chunks, total = [], 0
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise FetchError("timed out")
                if conn.sock is not None:
                    conn.sock.settimeout(left)  # a slow drip cannot outlive the deadline
                chunk = resp.read1(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise FetchError(f"response larger than {max_bytes} bytes")
                chunks.append(chunk)
            return FetchResult(current, resp.status, resp.getheader("Content-Type") or "", b"".join(chunks))
        except (OSError, http.client.HTTPException) as exc:
            raise FetchError(f"fetch failed: {exc.__class__.__name__}") from None
        finally:
            conn.close()
    raise FetchError("too many redirects")
