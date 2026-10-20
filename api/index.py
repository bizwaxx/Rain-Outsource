import json
from urllib.parse import urlparse
from http.server import BaseHTTPRequestHandler

from rainout_agent import http_cache
from rainout_agent.cache import get_cache
from rainout_agent.site_pages import ROOT_CACHE, landing_html, wants_html
from rainout_agent.status_api import list_supported_fields

BASE_URL = "https://rainout-agent-source.vercel.app"


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self._handle(include_body=True)

    def do_HEAD(self):
        self._handle(include_body=False)

    def _handle(self, include_body=True):
        self._include_body = include_body
        url = urlparse(self.path)
        is_root = url.path in ("", "/")
        if is_root:
            req_headers = getattr(self, "headers", None)
            accept = req_headers.get("Accept", "") if req_headers is not None else ""
            if wants_html(accept, url.query):
                return self._send(200, landing_html(), "text/html; charset=utf-8", ROOT_CACHE, vary=True)
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
        if is_root:
            # "/" also serves the HTML landing page, so it varies on Accept and skips the edge cache.
            return self._send(200, json.dumps(payload).encode("utf-8"), "application/json", ROOT_CACHE, vary=True)
        self._send_json(200, payload, http_cache.INDEX)

    def _send_json(self, status_code, payload, cache_control=http_cache.NO_STORE):
        self._send(status_code, json.dumps(payload).encode("utf-8"), "application/json", cache_control)

    def _send(self, status_code, body, content_type, cache_control, vary=False):
        self.send_response(status_code)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", cache_control)
        if vary:
            self.send_header("Vary", "Accept")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if getattr(self, "_include_body", True):
            self.wfile.write(body)
