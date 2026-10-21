import json

from rainout_agent import odds_label


def test_odds_answer_is_labelled_estimate_official_first_with_accuracy_link():
    payload = {"creator": "JEEZ Labs", "field": "X", "game_time_play_probability_percent": 72,
               "official_status": "unknown", "official_status_source_url": "https://example.org",
               "spoken_answer": "Rain chance is 30%."}
    out = odds_label.apply(payload)
    keys = list(out)
    assert keys[:3] == ["creator", "official_status", "official_status_source_url"]
    assert keys.index("odds_are") < keys.index("game_time_play_probability_percent")
    assert out["odds_are"] == "estimate" and out["accuracy_url"].endswith("/accuracy")
    assert "estimate" in out["spoken_answer"]


def test_nested_odds_labelled_and_non_odds_untouched():
    sched = {"games": [{"play_probability_percent": 40}]}
    assert odds_label.apply(sched)["odds_are"] == "estimate"
    rain = {"summary": "Up to 33% chance of rain", "rain_chance_percent": 33}
    assert "odds_are" not in odds_label.apply(rain)
    assert "odds_are" not in odds_label.apply({"error": "unknown_field", "play_probability": 5})
    spoken = odds_label.apply({"play_probability": 80, "spoken_answer": "Estimated play probability is 80%."})
    assert spoken["spoken_answer"].count("estimate") == 0 and "Estimated" in spoken["spoken_answer"]


def test_status_and_mcp_answers_carry_the_label():
    from rainout_agent.status_api import build_status_result
    weather = {"rain_chance_percent": 20, "thunderstorm_likely": False, "source": "test", "source_id": "nws"}
    from rainout_agent.status_api import list_supported_fields
    fid = list_supported_fields()[0]["field_id"]
    res = build_status_result(fid, "2026-05-02T19:00:00-05:00", weather=weather, official_status="unknown")
    assert res["odds_are"] == "estimate" and list(res).index("official_status") < list(res).index("game_time_play_probability_percent")
    assert odds_label.ACCURACY_URL in json.dumps(res)
