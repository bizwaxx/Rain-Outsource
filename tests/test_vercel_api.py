import io
import json
from http.server import BaseHTTPRequestHandler

from api.fields import handler as FieldsHandler
from api.index import handler as IndexHandler
from api.openapi import handler as OpenApiHandler
from api.status import handler as StatusHandler


class HandlerHarness:
    def __init__(self, handler_class, path):
        self.instance = handler_class.__new__(handler_class)
        self.instance.path = path
        self.instance.wfile = io.BytesIO()
        self.status_code = None
        self.headers = {}

        def send_response(code, message=None):
            self.status_code = code

        def send_header(name, value):
            self.headers[name] = value

        def end_headers():
            pass

        self.instance.send_response = send_response
        self.instance.send_header = send_header
        self.instance.end_headers = end_headers

    def get_json(self):
        self.instance.do_GET()
        return self.status_code, self.headers, json.loads(self.instance.wfile.getvalue().decode("utf-8"))

    def get_text(self):
        self.instance.do_GET()
        return self.status_code, self.headers, self.instance.wfile.getvalue().decode("utf-8")


def test_vercel_handlers_are_base_http_handlers():
    assert issubclass(FieldsHandler, BaseHTTPRequestHandler)
    assert issubclass(IndexHandler, BaseHTTPRequestHandler)
    assert issubclass(StatusHandler, BaseHTTPRequestHandler)
    assert issubclass(OpenApiHandler, BaseHTTPRequestHandler)


def test_vercel_index_handler_reports_service_ready_and_discovery_links():
    status_code, headers, body = HandlerHarness(IndexHandler, "/api").get_json()

    assert status_code == 200
    assert headers["Content-Type"] == "application/json"
    assert body["status"] == "ok"
    assert body["service"] == "rainout-source"
    assert body["openapi_url"] == "https://rainout-agent-source.vercel.app/openapi.yaml"
    assert body["fields_endpoint"] == "https://rainout-agent-source.vercel.app/v1/fields"
    assert body["status_endpoint"].startswith("https://rainout-agent-source.vercel.app/v1/status")
    field_ids = {field["field_id"] for field in body["supported_fields"]}
    assert "austin-tx-krieg-field-softball-complex" in field_ids
    assert "austin-tx-havins-softball-complex" in field_ids


def test_vercel_openapi_handler_serves_yaml_contract():
    status_code, headers, body = HandlerHarness(OpenApiHandler, "/openapi.yaml").get_text()

    assert status_code == 200
    assert headers["Content-Type"].startswith("application/yaml")
    assert "openapi: 3.0.3" in body
    assert "https://rainout-agent-source.vercel.app" in body
    assert "/v1/status:" in body
    assert "/v1/fields:" in body
    assert "version: 0.6.0" in body
    assert "answer_requirements" in body


def test_vercel_fields_handler_lists_supported_fields_for_agents():
    status_code, headers, body = HandlerHarness(FieldsHandler, "/v1/fields").get_json()

    assert status_code == 200
    assert headers["Content-Type"] == "application/json"
    assert body["service"] == "rainout-source"
    assert body["count"] >= 6
    fields = {field["field_id"]: field for field in body["fields"]}
    krieg = fields["austin-tx-krieg-field-softball-complex"]
    havins = fields["austin-tx-havins-softball-complex"]
    assert krieg["name"] == "Krieg Field Softball Complex"
    assert "Krieg" in krieg["aliases"]
    assert "Craig" in krieg["aliases"]
    assert havins["name"] == "Havins Softball Complex"
    assert "Havins" in havins["aliases"]
    assert "austin-metro-northeast-metropolitan-park" in fields
    assert "austin-tx-oak-hill-youth-sports-association" in fields
    assert krieg["status_url"].startswith("https://rainout-agent-source.vercel.app/v1/status?field_id=")


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

    status_code, headers, body = HandlerHarness(
        StatusHandler,
        "/api/status?field_id=Krieg&game_time=2026-06-06T20:20:00-05:00",
    ).get_json()

    assert status_code == 200
    assert headers["Content-Type"] == "application/json"
    assert body["field_id"] == "austin-tx-krieg-field-softball-complex"
    assert body["rain_chance_percent"] == 22
    assert "rain chance" in body["spoken_answer"].lower()
    assert body["answer_requirements"]["if_official_status_unknown"] == "Say official status is unknown and tell the user to call 512-978-2680 before leaving."


def test_vercel_status_handler_rejects_missing_game_time():
    status_code, headers, body = HandlerHarness(StatusHandler, "/api/status?field_id=Krieg").get_json()

    assert status_code == 400
    assert body["error"] == "missing_game_time"
