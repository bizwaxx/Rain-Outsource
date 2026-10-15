from http.server import BaseHTTPRequestHandler
from pathlib import Path

from rainout_agent import http_cache


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        root = Path(__file__).resolve().parents[1]
        body = (root / "openapi.yaml").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/yaml; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", http_cache.OPENAPI)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
