import json
from urllib.parse import urlencode

from api.index import handler as index_handler
from api.status import handler as status_handler


class FakeRequest:
    def __init__(self, path="/", query=None):
        self.path = path
        self.query = query or {}


class FakeResponse:
    def __init__(self):
        self.status_code = None
        self.headers = {}
        self.body = None

    def status(self, code):
        self.status_code = code
        return self

    def json(self, payload):
        self.headers["Content-Type"] = "application/json"
        self.body = payload
        return self


def test_vercel_index_handler_reports_service_ready():
    response = FakeResponse()

    index_handler(FakeRequest(), response)

    assert response.status_code == 200
    assert response.body["status"] == "ok"
    assert response.body["service"] == "rainout-source"


def test_vercel_status_handler_returns_krieg_status_with_mock_weather(monkeypatch):
    monkeypatch.setattr(
        "rainout_agent.status_api.fetch_nws_weather",
        lambda field, game_time: {
            "rain_chance_percent": 22,
            "thunderstorm_likely": False,
            "source": "National Weather Service API test data",
            "last_checked": "2026-06-06T08:00:00-05:00",
            "forecast": "Slight Chance Rain Showers",
        },
    )
    response = FakeResponse()

    status_handler(
        FakeRequest(query={"field_id": "Krieg", "game_time": "2026-06-06T20:20:00-05:00"}),
        response,
    )

    assert response.status_code == 200
    assert response.body["field_id"] == "austin-tx-krieg-field-softball-complex"
    assert response.body["rain_chance_percent"] == 22
    assert "rain chance" in response.body["spoken_answer"].lower()


def test_vercel_status_handler_rejects_missing_game_time():
    response = FakeResponse()

    status_handler(FakeRequest(query={"field_id": "Krieg"}), response)

    assert response.status_code == 400
    assert response.body["error"] == "missing_game_time"
