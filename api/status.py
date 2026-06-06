import json
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from rainout_agent.status_api import build_status_result


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
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
        return self._send_json(status_code, payload)

    def _send_json(self, status_code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _first(params, name):
    values = params.get(name) or []
    return values[0] if values else ""
