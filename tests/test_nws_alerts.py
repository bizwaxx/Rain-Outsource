from datetime import datetime, timedelta, timezone

from rainout_agent import event_risk as er, nws_alerts as na
from rainout_agent.cache import MemoryCache

NOW = datetime(2026, 5, 2, 20, 0, tzinfo=timezone.utc)


def _payload(*events):
    return {"features": [{"id": f"urn:{i}", "properties": {"event": ev, "headline": ev, "severity": "Severe",
                                                           "onset": (NOW - timedelta(hours=1)).isoformat(),
                                                           "ends": (NOW + timedelta(hours=3)).isoformat(),
                                                           "senderName": "NWS Austin/San Antonio TX"}}
                         for i, ev in enumerate(events)]}


def test_off_by_default(monkeypatch):
    monkeypatch.delenv("RAINOUT_NWS_ALERTS", raising=False)
    assert not na.enabled()
    assert na.live_features(30.2, -97.7, NOW + timedelta(hours=2), NOW) is None


def test_relevant_alerts_shape_matches_lightning_items():
    out = na.relevant_alerts(_payload("Severe Thunderstorm Warning", "Flood Watch", "Heat Advisory"))
    assert [a["event"] for a in out] == ["Severe Thunderstorm Warning", "Flood Watch"]
    assert {"event", "level", "headline", "severity", "onset", "ends", "sender", "url", "kind"} <= set(out[0])
    assert out[0]["kind"] == "warning" and out[1]["kind"] == "watch"


def test_features_and_live_cache(monkeypatch):
    monkeypatch.setenv("RAINOUT_NWS_ALERTS", "on")
    calls = []
    from rainout_agent import weather

    def fake(url, headers, timeout):
        calls.append(url)
        assert "User-Agent" in headers and url.startswith(na.NWS_ALERTS_URL + "?point=")
        return _payload("Tropical Storm Warning")

    monkeypatch.setattr(weather, "_http_get_json", fake)
    cache = MemoryCache()
    f = na.live_features(29.75, -95.35, NOW + timedelta(hours=1), NOW, cache=cache)
    assert f["alert_warn"] and f["alert_watch"] and f["alert_tropical"]
    na.live_features(29.75, -95.35, NOW + timedelta(hours=1), NOW, cache=cache)
    assert len(calls) == 1                                       # cached
    assert na.live_features(29.75, -95.35, NOW + timedelta(days=2), NOW, cache=cache) is None   # too far out


def test_caps_only_apply_when_params_set():
    feat = {"window_pop": 5, "alert_warn": True, "alert_watch": True, "alert_tropical": False, "alert_names": ["Tornado Warning"]}
    base = er.base_percent(5, "medium")
    assert er.adjust(base, feat, "medium")["play_probability_percent"] == base
    p = {**er.PARAMS, "alert_warn_cap": 45}
    out = er.adjust(base, feat, "medium", p)
    assert out["play_probability_percent"] == 45 and any("Tornado Warning" in r for r in out["reasons"])
