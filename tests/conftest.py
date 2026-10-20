import pytest


@pytest.fixture(autouse=True)
def _no_live_event_weather(monkeypatch):
    """The v2 event model fetches Open-Meteo live; keep the suite offline unless a test opts in."""
    monkeypatch.setenv("RAINOUT_EVENT_RISK", "off")
