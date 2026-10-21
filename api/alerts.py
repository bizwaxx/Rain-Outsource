from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse

from rainout_agent import metrics
from rainout_agent.alerts_api import MAX_BODY, handle
from rainout_agent.reports import client_from_headers


class handler(BaseHTTPRequestHandler):
    def _run(self, method):
        parsed = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(min(length, MAX_BODY + 1)) if length > 0 else b""
        headers = dict(self.headers.items())
        status, out, payload = handle(method, parsed.path, parsed.query, body, self.headers.get("Content-Type") or "",
                                      headers, client=client_from_headers(headers, (self.client_address or [None])[0]))
        self.send_response(status)
        for key, value in out.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload and method != "HEAD":
            self.wfile.write(payload)

    def do_GET(self):
        self._run("GET")

    def do_POST(self):
        self._run("POST")

    def do_DELETE(self):
        self._run("DELETE")

    def do_OPTIONS(self):
        self._run("OPTIONS")


metrics.instrument(handler)
