import json
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from rainout_agent import metrics
from rainout_agent import http_cache
from rainout_agent.alerts_api import MAX_BODY as ALERTS_MAX_BODY, handle as handle_alerts
from rainout_agent.reports import client_from_headers
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


def _is_alerts(path):
    p = path.rstrip("/")
    return p == "/v1/alerts" or p.startswith("/v1/alerts/")


class handler(BaseHTTPRequestHandler):
    # /v1/alerts/* shares this function to stay under the Vercel Hobby limit of 12 functions.
    def _alerts(self, method):
        parsed = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(min(length, ALERTS_MAX_BODY + 1)) if length > 0 else b""
        headers = dict(self.headers.items())
        status, out, payload = handle_alerts(method, parsed.path, parsed.query, body, self.headers.get("Content-Type") or "",
                                             headers, client=client_from_headers(headers, (self.client_address or [None])[0]))
        self.send_response(status)
        for key, value in out.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload and method != "HEAD":
            self.wfile.write(payload)

    def _not_allowed(self):
        self._send_json(405, {"error": "method_not_allowed"})

    def do_POST(self):
        if _is_alerts(urlparse(self.path).path):
            return self._alerts("POST")
        return self._not_allowed()

    def do_DELETE(self):
        if _is_alerts(urlparse(self.path).path):
            return self._alerts("DELETE")
        return self._not_allowed()

    def do_OPTIONS(self):
        if _is_alerts(urlparse(self.path).path):
            return self._alerts("OPTIONS")
        return self._not_allowed()

    def do_GET(self):
        if _is_alerts(urlparse(self.path).path):
            return self._alerts("GET")
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
