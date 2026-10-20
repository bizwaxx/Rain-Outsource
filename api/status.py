import json
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from rainout_agent import metrics
from rainout_agent import http_cache
from rainout_agent.premium_api import MAX_BODY as PREMIUM_MAX, handle_premium_request, is_premium_path
from rainout_agent.reports_api import MAX_BODY, handle_reports_request
from rainout_agent.status_api import build_status_result


class handler(BaseHTTPRequestHandler):
    # Paid-tier paths (/v1/plans, /v1/account, /v1/batch, /v1/billing/*) share this function so the
    # project needs no extra Vercel function. Existing paths never reach the paid layer.
    def _premium(self, method):
        parsed = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(min(length, PREMIUM_MAX + 1)) if length > 0 else b""
        status, headers, payload = handle_premium_request(method, parsed.path, parsed.query, body, dict(self.headers.items()),
                                                          client=(getattr(self, "client_address", None) or [None])[0])
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    # /v1/reports is served by this function so that, with the default in-memory cache,
    # reports land on the same instances that answer /v1/status.
    def _reports(self, method):
        parsed = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(min(length, MAX_BODY + 1)) if length > 0 else b""
        status, headers, payload = handle_reports_request(
            method, parsed.query, body, self.headers.get("Content-Type") or "", dict(self.headers.items()),
            client=(getattr(self, "client_address", None) or [None])[0])
        self.send_response(status)
        self.send_header("Access-Control-Allow-Origin", "*")
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def do_POST(self):
        if is_premium_path(urlparse(self.path).path):
            return self._premium("POST")
        if urlparse(self.path).path.rstrip("/") == "/v1/reports":
            return self._reports("POST")
        return self._send_json(405, {"error": "method_not_allowed"})

    def do_OPTIONS(self):
        if is_premium_path(urlparse(self.path).path):
            return self._premium("OPTIONS")
        return self._reports("OPTIONS")

    def do_GET(self):
        parsed = urlparse(self.path)
        if is_premium_path(parsed.path):
            return self._premium("GET")
        if parsed.path.rstrip("/") == "/v1/reports":
            return self._reports("GET")
        params = parse_qs(parsed.query, keep_blank_values=True)
        field_query = _first(params, "field_id") or _first(params, "field")
        game_time = _first(params, "game_time")

        if not field_query:
            return self._send_json(400, {"error": "missing_field_id"})
        if not game_time:
            return self._send_json(400, {"error": "missing_game_time"})

        try:
            payload = build_status_result(field_query=field_query, game_time=game_time)
        except Exception as exc:  # pragma: no cover - live host safety net
            return self._send_json(
                502,
                {
                    "creator": "JEEZ Labs",
                    "error": "weather_source_unavailable",
                    "message": str(exc),
                    "spoken_answer": "I could not reach the live weather source right now. Please check the official rainout line before leaving.",
                },
            )

        status_code = 404 if payload.get("error") == "unknown_field" else 200
        return self._send_json(status_code, payload, http_cache.status_cache_control(status_code, payload))

    def _send_json(self, status_code, payload, cache_control=http_cache.NO_STORE):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", cache_control)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _first(params, name):
    values = params.get(name) or []
    return values[0] if values else ""


metrics.instrument(handler)
