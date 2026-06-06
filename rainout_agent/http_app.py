from __future__ import annotations

import json
from urllib.parse import parse_qs
from wsgiref.simple_server import make_server

from rainout_agent.status_api import build_status_result, list_supported_fields


def _json_response(start_response, status: str, payload: dict):
    body = json.dumps(payload, indent=2).encode("utf-8")
    start_response(
        status,
        [
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(body))),
            ("Access-Control-Allow-Origin", "*"),
        ],
    )
    return [body]


def application(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET")
    path = environ.get("PATH_INFO", "/")

    if method == "OPTIONS":
        return _json_response(start_response, "200 OK", {"status": "ok"})

    if method != "GET":
        return _json_response(start_response, "405 Method Not Allowed", {"error": "method_not_allowed"})

    if path == "/health":
        return _json_response(start_response, "200 OK", {"status": "ok", "service": "rainout-source"})

    if path == "/v1/fields":
        fields = list_supported_fields()
        return _json_response(
            start_response,
            "200 OK",
            {
                "service": "rainout-source",
                "creator": "JEEZ Labs",
                "count": len(fields),
                "fields": fields,
            },
        )

    if path != "/v1/status":
        return _json_response(start_response, "404 Not Found", {"error": "not_found"})

    params = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)
    field_query = (params.get("field_id") or params.get("field") or [""])[0]
    game_time = (params.get("game_time") or [""])[0]

    if not field_query:
        return _json_response(start_response, "400 Bad Request", {"error": "missing_field_id"})
    if not game_time:
        return _json_response(start_response, "400 Bad Request", {"error": "missing_game_time"})

    try:
        payload = build_status_result(field_query=field_query, game_time=game_time)
    except Exception as exc:  # pragma: no cover - safety net for live hosting
        return _json_response(
            start_response,
            "502 Bad Gateway",
            {
                "creator": "JEEZ Labs",
                "error": "weather_source_unavailable",
                "message": str(exc),
                "spoken_answer": "I could not reach the live weather source right now. Please check the official rainout line before leaving.",
            },
        )

    status = "404 Not Found" if payload.get("error") == "unknown_field" else "200 OK"
    return _json_response(start_response, status, payload)


def main():
    host = "127.0.0.1"
    port = 8000
    print(f"Rainout Source API running at http://{host}:{port}")
    print("Try: /v1/status?field_id=Krieg&game_time=2026-06-05T20:20:00-05:00")
    with make_server(host, port, application) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
