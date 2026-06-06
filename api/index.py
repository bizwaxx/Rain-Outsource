import json
from http.server import BaseHTTPRequestHandler

from rainout_agent.status_api import list_supported_fields

BASE_URL = "https://rainout-agent-source.vercel.app"


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
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
        self._send_json(200, payload)

    def _send_json(self, status_code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
