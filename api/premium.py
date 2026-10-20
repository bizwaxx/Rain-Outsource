from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse

from rainout_agent.premium_api import MAX_BODY, handle_premium_request


class handler(BaseHTTPRequestHandler):
    """Paid-tier endpoints (/v1/plans, /v1/account, /v1/batch). Existing endpoints are untouched."""

    def _handle(self, method):
        parsed = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(min(length, MAX_BODY + 1)) if length > 0 else b""
        status, headers, payload = handle_premium_request(method, parsed.path, parsed.query, body, dict(self.headers.items()))
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_OPTIONS(self):
        self._handle("OPTIONS")
