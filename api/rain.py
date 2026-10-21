from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse

from rainout_agent import metrics
from rainout_agent.lightning_api import handle_lightning_request, is_lightning_path
from rainout_agent.rain_api import handle_rain_request


class handler(BaseHTTPRequestHandler):
    # /v1/lightning shares this function (Vercel Hobby has a function budget).
    def do_GET(self, method="GET"):
        parsed = urlparse(self.path)
        if is_lightning_path(parsed.path):
            status, content_type, body, cache_control = handle_lightning_request(method, parsed.path, parsed.query, dict(self.headers.items()))
        else:
            status, content_type, body, cache_control = handle_rain_request(method, parsed.path, parsed.query)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", cache_control)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        return self.do_GET("POST")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()


metrics.instrument(handler)
