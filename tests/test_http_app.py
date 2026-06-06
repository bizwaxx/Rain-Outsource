import json

from rainout_agent.http_app import application


def call_app(path, query=""):
    status_headers = {}

    def start_response(status, headers):
        status_headers["status"] = status
        status_headers["headers"] = dict(headers)

    body = b"".join(
        application(
            {
                "REQUEST_METHOD": "GET",
                "PATH_INFO": path,
                "QUERY_STRING": query,
            },
            start_response,
        )
    )
    return status_headers, json.loads(body.decode("utf-8"))


def test_status_endpoint_returns_krieg_json_with_mock_weather(monkeypatch):
    monkeypatch.setattr(
        "rainout_agent.status_api.fetch_nws_weather",
        lambda field, game_time: {
            "rain_chance_percent": 40,
            "thunderstorm_likely": False,
            "source": "National Weather Service API test data",
            "last_checked": "2026-06-05T19:00:00-05:00",
            "forecast": "Chance Rain Showers",
        },
    )

    status, data = call_app("/v1/status", "field_id=Krieg&game_time=2026-06-05T20:20:00-05:00")

    assert status["status"].startswith("200")
    assert status["headers"]["Content-Type"] == "application/json"
    assert data["field_id"] == "austin-tx-krieg-field-softball-complex"
    assert data["rain_chance_percent"] == 40
    assert "rain chance" in data["spoken_answer"].lower()


def test_status_endpoint_rejects_missing_game_time():
    status, data = call_app("/v1/status", "field_id=Krieg")

    assert status["status"].startswith("400")
    assert data["error"] == "missing_game_time"


def test_health_endpoint_is_ready():
    status, data = call_app("/health")

    assert status["status"].startswith("200")
    assert data["status"] == "ok"
