from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse

from rainout_agent.construction_api import MAX_BODY, handle_construction_request


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self._respond("GET", b"")

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(min(length, MAX_BODY + 1)) if length > 0 else b""
        self._respond("POST", body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-API-Key, Authorization")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _respond(self, method, body):
        parsed = urlparse(self.path)
        status, content_type, payload, cache_control = handle_construction_request(
            method, parsed.path, parsed.query, body, self.headers.get("Content-Type", "") if getattr(self, "headers", None) else "",
            headers=dict(self.headers.items()) if getattr(self, "headers", None) is not None else {})
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", cache_control)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
