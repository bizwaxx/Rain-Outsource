import json
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from rainout_agent import metrics
from rainout_agent import http_cache
from rainout_agent.status_api import fields_payload
from rainout_agent.venue_schema import SPORTS


def build_fields_payload(sport=None, state=None, **kw):
    return fields_payload(sport=sport, state=state, **kw)


def invalid_filter_payload(message):
    return {
        "error": "invalid_filter",
        "message": message,
        "allowed_sports": list(SPORTS),
        "state_format": "Two-letter US state code, e.g. TX; comma-separate several (TX,OK).",
        "params": "q, near=lat,lon, radius_km (1-200), verification=confirmed|osm_only|all, limit (1-500), offset",
    }


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        params = parse_qs(urlparse(self.path).query, keep_blank_values=True)
        args = {k: (params.get(k) or [None])[0] for k in ("sport", "state", "q", "near", "radius_km", "verification", "limit", "offset")}
        try:
            payload = build_fields_payload(**args)
        except ValueError as exc:
            return self._send_json(400, invalid_filter_payload(str(exc)))
        self._send_json(200, payload, http_cache.fields_cache_control(200))

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
