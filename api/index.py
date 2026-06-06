def handler(request, response):
    return response.status(200).json(
        {
            "status": "ok",
            "service": "rainout-source",
            "creator": "JEEZ Labs",
            "status_endpoint": "/api/status?field_id=Krieg&game_time=2026-06-06T20:20:00-05:00",
        }
    )
