import json
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from rainout_agent.status_api import list_supported_fields
from rainout_agent.venue_schema import SPORTS


def build_fields_payload(sport=None, state=None):
    fields = list_supported_fields(sport=sport, state=state)
    payload = {
        "service": "rainout-source",
        "creator": "JEEZ Labs",
        "count": len(fields),
        "fields": fields,
    }
    filters = {key: value for key, value in (("sport", sport), ("state", state)) if value}
    if filters:
        payload["filters"] = filters
    return payload


def invalid_filter_payload(message):
    return {
        "error": "invalid_filter",
        "message": message,
        "allowed_sports": list(SPORTS),
        "state_format": "Two-letter US state code, e.g. TX; comma-separate several (TX,OK).",
    }


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        params = parse_qs(urlparse(self.path).query, keep_blank_values=True)
        sport = (params.get("sport") or [None])[0]
        state = (params.get("state") or [None])[0]
        try:
            payload = build_fields_payload(sport=sport, state=state)
        except ValueError as exc:
            return self._send_json(400, invalid_filter_payload(str(exc)))
        self._send_json(200, payload)

    def _send_json(self, status_code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
