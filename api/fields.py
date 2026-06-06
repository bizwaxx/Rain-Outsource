import json
from http.server import BaseHTTPRequestHandler
from urllib.parse import quote

from rainout_agent.status_api import FIELD_ID, load_field

BASE_URL = "https://rainout-agent-source.vercel.app"


def build_fields_payload():
    field = load_field(FIELD_ID)
    aliases = ["Krieg", "Krieg Field", "Krieg Softball", FIELD_ID]
    status_url = f"{BASE_URL}/v1/status?field_id={quote('Krieg')}&game_time={{ISO-8601-game-time}}"
    return {
        "service": "rainout-source",
        "creator": "JEEZ Labs",
        "count": 1,
        "fields": [
            {
                "field_id": FIELD_ID,
                "name": field["field_name"],
                "city": field["city"],
                "state": field["state"],
                "address": field["address"],
                "sport": field["sport"],
                "rainout_phone": field["rainout_phone"],
                "aliases": aliases,
                "weather_source": field["weather_source"],
                "status_url": status_url,
            }
        ],
    }


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self._send_json(200, build_fields_payload())

    def _send_json(self, status_code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
