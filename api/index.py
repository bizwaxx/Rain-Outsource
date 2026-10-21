import json
from urllib.parse import urlparse
from http.server import BaseHTTPRequestHandler

from rainout_agent import metrics
from rainout_agent import http_cache
from rainout_agent.cache import get_cache
from rainout_agent.status_api import list_supported_fields

BASE_URL = "https://rainout-agent-source.vercel.app"


def _is_demo(path):
    p = urlparse(path or "/").path.rstrip("/")
    return p == "/demo" or p.startswith("/demo/")


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if _is_demo(self.path):
            return self._demo("GET")
        payload = {
            "status": "ok",
            "service": "rainout-source",
            "creator": "JEEZ Labs",
            "description": "Open public API for rainout and game-time play probability. Pilot fields include Krieg/Craig, Havins, and an Austin metro public/private baseball and softball expansion batch.",
            "live_base_url": BASE_URL,
            "openapi_url": f"{BASE_URL}/openapi.yaml",
            "fields_endpoint": f"{BASE_URL}/v1/fields",
            "docs_url": "https://bizwaxx.github.io/Rain-Outsource/",
            "status_endpoint": f"{BASE_URL}/v1/status?field_id=Krieg&game_time=2026-06-06T20:20:00-05:00",
            "supported_fields": list_supported_fields(),
        }
        if urlparse(self.path).path.rstrip("/") == "/health":
            payload["weather_cache"] = get_cache().describe()
            return self._send_json(200, payload, http_cache.NO_STORE)
        self._send_json(200, payload, http_cache.INDEX)

    def do_POST(self):
        if _is_demo(self.path):
            return self._demo("POST")
        self._send_json(405, {"error": "method_not_allowed"})

    def _demo(self, method):
        from rainout_agent import demo

        url = urlparse(self.path)
        body = b""
        if method == "POST":
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                length = 0
            body = self.rfile.read(min(length, 4096)) if length > 0 else b""
        status, headers, payload = demo.handle(method, url.path, url.query, body,
                                               self.headers.get("Content-Type") or "", self.headers.get("Accept") or "")
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, status_code, payload, cache_control=http_cache.NO_STORE):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", cache_control)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


metrics.instrument(handler)
