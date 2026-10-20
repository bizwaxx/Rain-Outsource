from http.server import BaseHTTPRequestHandler

from rainout_agent import metrics

from rainout_agent.site_pages import lookup


class handler(BaseHTTPRequestHandler):
    def _serve(self, include_body: bool):
        found = lookup(self.path)
        if found is None:
            status, ctype, body, cache = 404, "application/json", b'{"error": "not_found"}', "no-store"
        else:
            status, ctype, body, cache = found
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if include_body:
            self.wfile.write(body)

    def do_GET(self):
        self._serve(True)

    def do_HEAD(self):
        self._serve(False)


metrics.instrument(handler)
