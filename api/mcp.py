from http.server import BaseHTTPRequestHandler

from rainout_agent import metrics
from rainout_agent.mcp_server import MAX_BODY, handle_http


class handler(BaseHTTPRequestHandler):
    def _run(self, method):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(min(length, MAX_BODY + 1)) if length > 0 else b""
        status, headers, payload = handle_http(method, dict(self.headers.items()), body)
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def do_POST(self):
        self._run("POST")

    def do_GET(self):
        self._run("GET")

    def do_DELETE(self):
        self._run("DELETE")

    def do_OPTIONS(self):
        self._run("OPTIONS")


metrics.instrument(handler)
