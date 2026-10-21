from __future__ import annotations

import json
from urllib.parse import parse_qs
from wsgiref.simple_server import make_server

from rainout_agent import http_cache
from rainout_agent.cache import get_cache
from rainout_agent.status_api import build_status_result, list_supported_fields
from rainout_agent.schedule_api import MAX_BODY_BYTES, handle_schedule_request
from rainout_agent.venue_schema import SPORTS


def _json_response(start_response, status: str, payload: dict, cache_control: str = http_cache.NO_STORE):
    body = json.dumps(payload, indent=2).encode("utf-8")
    start_response(
        status,
        [
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(body))),
            ("Access-Control-Allow-Origin", "*"),
            ("Cache-Control", cache_control),
        ],
    )
    return [body]


def application(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET")
    path = environ.get("PATH_INFO", "/")

    if path.rstrip("/") == "/mcp":
        from rainout_agent.mcp_server import MAX_BODY, handle_http

        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = 0
        body = environ["wsgi.input"].read(min(length, MAX_BODY + 1)) if length > 0 else b""
        headers = {k[5:].replace("_", "-").title(): v for k, v in environ.items() if k.startswith("HTTP_")}
        status, out_headers, payload = handle_http(method, headers, body)
        reasons = {200: "OK", 202: "Accepted", 204: "No Content", 400: "Bad Request", 403: "Forbidden", 405: "Method Not Allowed", 413: "Payload Too Large"}
        start_response(f"{status} {reasons.get(status, 'Error')}", list(out_headers.items()) + [("Content-Length", str(len(payload)))])
        return [payload]

    if path.rstrip("/") == "/v1/reports":
        from http import HTTPStatus

        from rainout_agent.reports_api import MAX_BODY as REPORT_MAX, handle_reports_request

        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = 0
        body = environ["wsgi.input"].read(min(length, REPORT_MAX + 1)) if length > 0 else b""
        headers = {k[5:].replace("_", "-").title(): v for k, v in environ.items() if k.startswith("HTTP_")}
        status, out_headers, payload = handle_reports_request(method, environ.get("QUERY_STRING", ""), body,
                                                              environ.get("CONTENT_TYPE", ""), headers,
                                                              client=environ.get("REMOTE_ADDR"))
        out_headers = {**out_headers, "Access-Control-Allow-Origin": "*"}
        start_response(f"{status} {HTTPStatus(status).phrase}", list(out_headers.items()) + [("Content-Length", str(len(payload)))])
        return [payload]

    from rainout_agent.premium_api import is_premium_path

    if is_premium_path(path):
        from http import HTTPStatus

        from rainout_agent.premium_api import MAX_BODY as PREMIUM_MAX, handle_premium_request

        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = 0
        body = environ["wsgi.input"].read(min(length, PREMIUM_MAX + 1)) if length > 0 else b""
        headers = {k[5:].replace("_", "-").title(): v for k, v in environ.items() if k.startswith("HTTP_")}
        status, out_headers, payload = handle_premium_request(method, path, environ.get("QUERY_STRING", ""), body, headers,
                                                              client=environ.get("REMOTE_ADDR"))
        start_response(f"{status} {HTTPStatus(status).phrase}", list(out_headers.items()) + [("Content-Length", str(len(payload)))])
        return [payload]

    if path == "/llms.txt":
        from pathlib import Path

        body = (Path(__file__).resolve().parents[1] / "llms.txt").read_bytes()
        start_response("200 OK", [("Content-Type", "text/plain; charset=utf-8"), ("Content-Length", str(len(body))),
                                  ("Access-Control-Allow-Origin", "*"), ("Cache-Control", "public, max-age=3600, s-maxage=86400")])
        return [body]

    if method in {"GET", "HEAD"}:
        from rainout_agent import site_pages

        found = site_pages.lookup(path)
        if found is None and path == "/" and site_pages.wants_html(environ.get("HTTP_ACCEPT", ""), environ.get("QUERY_STRING", "")):
            found = (200, "text/html; charset=utf-8", site_pages.landing_html(), site_pages.ROOT_CACHE)
        if found is not None:
            code, ctype, body, cache = found
            start_response(f"{code} OK", [("Content-Type", ctype), ("Content-Length", str(len(body))),
                                          ("Access-Control-Allow-Origin", "*"), ("Cache-Control", cache), ("Vary", "Accept")])
            return [b"" if method == "HEAD" else body]

    if method == "OPTIONS":
        return _json_response(start_response, "200 OK", {"status": "ok"})

    if path == "/v1/schedule/check":
        body = b""
        if method == "POST":
            try:
                length = int(environ.get("CONTENT_LENGTH") or 0)
            except ValueError:
                length = 0
            body = environ["wsgi.input"].read(min(length, MAX_BODY_BYTES + 1)) if length > 0 else b""
        status, content_type, payload = handle_schedule_request(
            method, environ.get("QUERY_STRING", ""), body, environ.get("CONTENT_TYPE", "")
        )
        reason = {200: "OK", 400: "Bad Request", 405: "Method Not Allowed", 413: "Payload Too Large", 502: "Bad Gateway"}
        start_response(
            f"{status} {reason.get(status, 'Error')}",
            [
                ("Content-Type", content_type),
                ("Content-Length", str(len(payload))),
                ("Access-Control-Allow-Origin", "*"),
                ("Cache-Control", http_cache.schedule_cache_control(method, status)),
            ],
        )
        return [payload]

    if path.startswith("/v1/teams/"):
        from rainout_agent.teams_api import handle_teams_request

        status, content_type, payload, cache_control = handle_teams_request(method, path, environ.get("QUERY_STRING", ""))
        reason = {200: "OK", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed", 502: "Bad Gateway"}
        start_response(
            f"{status} {reason.get(status, 'Error')}",
            [
                ("Content-Type", content_type),
                ("Content-Length", str(len(payload))),
                ("Access-Control-Allow-Origin", "*"),
                ("Cache-Control", cache_control),
            ],
        )
        return [payload]

    if path == "/v1/tracks" or path.startswith("/v1/tracks/"):
        from rainout_agent.tracks_api import handle_tracks_request

        status, content_type, payload, cache_control = handle_tracks_request(method, path, environ.get("QUERY_STRING", ""))
        reason = {200: "OK", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed", 502: "Bad Gateway"}
        start_response(
            f"{status} {reason.get(status, 'Error')}",
            [
                ("Content-Type", content_type),
                ("Content-Length", str(len(payload))),
                ("Access-Control-Allow-Origin", "*"),
                ("Cache-Control", cache_control),
            ],
        )
        return [payload]

    from rainout_agent.construction_api import ROUTES as CONSTRUCTION_ROUTES

    if path.rstrip("/") in CONSTRUCTION_ROUTES:
        from http import HTTPStatus

        from rainout_agent.construction_api import MAX_BODY as C_MAX, handle_construction_request

        body = b""
        if method == "POST":
            try:
                length = int(environ.get("CONTENT_LENGTH") or 0)
            except ValueError:
                length = 0
            body = environ["wsgi.input"].read(min(length, C_MAX + 1)) if length > 0 else b""
        status, content_type, payload, cache_control = handle_construction_request(
            method, path, environ.get("QUERY_STRING", ""), body, environ.get("CONTENT_TYPE", ""),
            headers={k[5:].replace("_", "-").title(): v for k, v in environ.items() if k.startswith("HTTP_")})
        start_response(f"{status} {HTTPStatus(status).phrase}",
                       [("Content-Type", content_type), ("Content-Length", str(len(payload))),
                        ("Access-Control-Allow-Origin", "*"), ("Cache-Control", cache_control)])
        return [payload]

    if path.rstrip("/") in ("/v1/lightning", "/v1/lightning/ingest"):
        from http import HTTPStatus

        from rainout_agent.lightning_api import handle_lightning_request

        hdrs = {k[5:].replace("_", "-").title(): v for k, v in environ.items() if k.startswith("HTTP_")}
        status, content_type, payload, cache_control = handle_lightning_request(method, path, environ.get("QUERY_STRING", ""), hdrs)
        start_response(f"{status} {HTTPStatus(status).phrase}",
                       [("Content-Type", content_type), ("Content-Length", str(len(payload))),
                        ("Access-Control-Allow-Origin", "*"), ("Cache-Control", cache_control)])
        return [payload]

    if path.rstrip("/") == "/v1/rain":
        from rainout_agent.rain_api import handle_rain_request

        status, content_type, payload, cache_control = handle_rain_request(method, path, environ.get("QUERY_STRING", ""))
        reason = {200: "OK", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed", 502: "Bad Gateway"}
        start_response(
            f"{status} {reason.get(status, 'Error')}",
            [
                ("Content-Type", content_type),
                ("Content-Length", str(len(payload))),
                ("Access-Control-Allow-Origin", "*"),
                ("Cache-Control", cache_control),
            ],
        )
        return [payload]

    if method != "GET":
        return _json_response(start_response, "405 Method Not Allowed", {"error": "method_not_allowed"})

    if path == "/health":
        return _json_response(start_response, "200 OK", {"status": "ok", "service": "rainout-source", "weather_cache": get_cache().describe()})

    if path == "/v1/fields":
        from rainout_agent.status_api import fields_payload

        params = {k: v[0] for k, v in parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True).items()}
        try:
            payload = fields_payload(**{k: params.get(k) for k in ("sport", "state", "q", "near", "radius_km", "verification", "limit", "offset")})
        except ValueError as exc:
            return _json_response(
                start_response,
                "400 Bad Request",
                {"error": "invalid_filter", "message": str(exc), "allowed_sports": list(SPORTS)},
            )
        payload["protocol_version"] = "v0.9.0"
        return _json_response(start_response, "200 OK", payload, http_cache.FIELDS)

    if path == "/openapi.yaml":
        from pathlib import Path

        body = (Path(__file__).resolve().parents[1] / "openapi.yaml").read_bytes()
        start_response(
            "200 OK",
            [
                ("Content-Type", "application/yaml; charset=utf-8"),
                ("Content-Length", str(len(body))),
                ("Access-Control-Allow-Origin", "*"),
                ("Cache-Control", http_cache.OPENAPI),
            ],
        )
        return [body]

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

    code = 404 if payload.get("error") == "unknown_field" else 200
    status = "404 Not Found" if code == 404 else "200 OK"
    return _json_response(start_response, status, payload, http_cache.status_cache_control(code, payload))


def main():
    host = "127.0.0.1"
    port = 8000
    print(f"Rainout Source API running at http://{host}:{port}")
    print("Try: /v1/status?field_id=Krieg&game_time=2026-06-05T20:20:00-05:00")
    with make_server(host, port, application) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
