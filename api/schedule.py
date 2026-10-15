from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse

from rainout_agent import http_cache
from rainout_agent.schedule_api import MAX_BODY_BYTES, handle_schedule_request


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self._respond("GET", b"")

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY_BYTES:
            body = b"x" * (MAX_BODY_BYTES + 1)  # let the shared handler return 413
        else:
            body = self.rfile.read(length) if length > 0 else b""
        self._respond("POST", body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Cache-Control", http_cache.NO_STORE)
        self._cors()
        self.end_headers()

    def _respond(self, method, body):
        status, content_type, payload = handle_schedule_request(
            method,
            urlparse(self.path).query,
            body,
            self.headers.get("Content-Type", "") if getattr(self, "headers", None) else "",
        )
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", http_cache.schedule_cache_control(method, status))
        self._cors()
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
