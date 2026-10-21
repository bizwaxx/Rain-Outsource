import json
from urllib.parse import urlparse
from http.server import BaseHTTPRequestHandler

from rainout_agent import discovery_pages
from rainout_agent import metrics
from rainout_agent import http_cache
from rainout_agent.cache import get_cache
from rainout_agent.status_api import list_supported_fields

BASE_URL = "https://rainout-agent-source.vercel.app"


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self._send_page():
            return
        payload = {
            "status": "ok",
            "service": "rainout-source",
            "creator": "JEEZ Labs",
            "description": "Open public API for rainout and game-time play probability. Pilot fields include Krieg/Craig, Havins, and an Austin metro public/private baseball and softball expansion batch.",
            "live_base_url": BASE_URL,
            "openapi_url": f"{BASE_URL}/openapi.yaml",
            "fields_endpoint": f"{BASE_URL}/v1/fields",
            "docs_url": "https://bizwaxx.github.io/Rain-Outsource/",
            "status_endpoint": f"{BASE_URL}/v1/status?field_id=NWLL&game_time=2026-06-06T19:00:00-05:00",
            "supported_fields": list_supported_fields(),
        }
        if urlparse(self.path).path.rstrip("/") == "/health":
            payload["weather_cache"] = get_cache().describe()
            return self._send_json(200, payload, http_cache.NO_STORE)
        self._send_json(200, payload, http_cache.INDEX)

    def do_HEAD(self):
        if not self._send_page(head=True):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

    def _send_page(self, head=False):
        """Serve /privacy, /terms, /icon.png and the MCP server card from this function."""
        page = discovery_pages.lookup(self.path)
        if page is None:
            return False
        status, ctype, body, cache_control = page
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head:
            self.wfile.write(body)
        return True

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
