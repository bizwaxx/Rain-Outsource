from rainout_agent.status_api import build_status_result


def _query_value(request, name):
    value = getattr(request, "query", {}).get(name)
    if isinstance(value, list):
        return value[0] if value else ""
    return value or ""


def handler(request, response):
    field_query = _query_value(request, "field_id") or _query_value(request, "field")
    game_time = _query_value(request, "game_time")

    if not field_query:
        return response.status(400).json({"error": "missing_field_id"})
    if not game_time:
        return response.status(400).json({"error": "missing_game_time"})

    try:
        payload = build_status_result(field_query=field_query, game_time=game_time)
    except Exception as exc:  # pragma: no cover - live host safety net
        return response.status(502).json(
            {
                "creator": "JEEZ Labs",
                "error": "weather_source_unavailable",
                "message": str(exc),
                "spoken_answer": "I could not reach the live weather source right now. Please check the official rainout line before leaving.",
            }
        )

    status_code = 404 if payload.get("error") == "unknown_field" else 200
    return response.status(status_code).json(payload)
