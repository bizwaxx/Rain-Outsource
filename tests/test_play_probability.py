from rainout_agent.play_probability import calculate_play_probability


def test_official_cancelled_returns_zero_play_probability():
    result = calculate_play_probability(
        official_status="cancelled",
        precipitation_chance_percent=20,
        thunderstorm_likely=False,
        hours_until_game=1.0,
    )

    assert result["play_probability_percent"] == 0
    assert result["recommendation"] == "do not drive"
    assert "official status is cancelled" in result["reason"].lower()


def test_official_field_closed_with_api_value_returns_do_not_drive():
    result = calculate_play_probability(
        official_status="field_closed",
        precipitation_chance_percent=20,
        thunderstorm_likely=False,
        hours_until_game=1.0,
    )

    assert result["play_probability_percent"] == 0
    assert result["risk_level"] == "officially cancelled"
    assert result["recommendation"] == "do not drive"


def test_official_on_with_high_storm_chance_flags_risky_drive():
    result = calculate_play_probability(
        official_status="on",
        precipitation_chance_percent=67,
        thunderstorm_likely=True,
        hours_until_game=1.0,
    )

    assert result["play_probability_percent"] == 25
    assert result["risk_level"] == "high"
    assert result["recommendation"] == "check again before leaving"


def test_official_on_with_low_rain_chance_says_likely_playing():
    result = calculate_play_probability(
        official_status="on",
        precipitation_chance_percent=15,
        thunderstorm_likely=False,
        hours_until_game=0.5,
    )

    assert result["play_probability_percent"] == 85
    assert result["risk_level"] == "low"
    assert result["recommendation"] == "likely safe to drive"


def test_unknown_status_with_dry_forecast_is_high_baseline():
    result = calculate_play_probability(
        official_status="unknown",
        precipitation_chance_percent=10,
        thunderstorm_likely=False,
        hours_until_game=2.0,
    )

    assert result["play_probability_percent"] == 96
    assert result["risk_level"] == "unknown"
    assert result["recommendation"] == "check official source"
    assert "not a factor" in result["reason"] and "official source" in result["reason"]


def _unknown(rain, storm=False, cal=None):
    return calculate_play_probability("unknown", rain, storm, 12.0, historical_calibration=cal)


def test_unknown_status_example_numbers():
    assert _unknown(0)["play_probability_percent"] == 96
    assert _unknown(30)["play_probability_percent"] == 78
    assert _unknown(50)["play_probability_percent"] == 60
    assert _unknown(70)["play_probability_percent"] == 42
    assert _unknown(100)["play_probability_percent"] == 15
    assert _unknown(40, storm=True)["play_probability_percent"] == 50
    assert _unknown(70, storm=True)["play_probability_percent"] == 34


def test_unknown_status_decreases_smoothly_with_rain():
    values = [_unknown(r)["play_probability_percent"] for r in range(0, 101)]
    assert all(b <= a for a, b in zip(values, values[1:]))
    assert all(a - b <= 2 for a, b in zip(values, values[1:]))  # no cliffs
    assert values[0] == values[10] == 96


def test_unknown_status_storms_cap_at_fifty():
    for rain in (0, 10, 30):
        assert _unknown(rain, storm=True)["play_probability_percent"] <= 50
        assert "Thunderstorms are likely" in _unknown(rain, storm=True)["reason"]


def test_unknown_status_uses_noaa_calibration_but_not_for_storms():
    cal = {"cancel_given_wet": 0.3, "month": "05", "rain_to_cancel_threshold_in": 0.5}
    plain, calibrated = _unknown(70), _unknown(70, cal=cal)
    assert calibrated["play_probability_percent"] > plain["play_probability_percent"]
    assert calibrated["rain_severity"] == 0.825  # NOAA severity 0.65 at half weight
    assert calibrated["play_probability_percent"] <= 60  # still clearly a rain risk
    assert "NOAA records" in calibrated["reason"]
    assert _unknown(70, True, cal)["play_probability_percent"] == _unknown(70, True)["play_probability_percent"]


def test_unknown_never_beats_baseline_and_cancelled_and_on_unchanged():
    assert max(_unknown(r)["play_probability_percent"] for r in range(101)) == 96
    assert calculate_play_probability("cancelled", 0, False, 1)["play_probability_percent"] == 0
    assert calculate_play_probability("on", 30, False, 1)["play_probability_percent"] == 70
    assert calculate_play_probability("on", 0, False, 1)["play_probability_percent"] == 100


def test_delayed_or_conflicting_status_is_genuinely_uncertain():
    for status in ("delayed", "conflicting"):
        result = calculate_play_probability(status, 0, False, 1.0)
        assert result["play_probability_percent"] == 50
        assert result["recommendation"] == "check official source"
    assert calculate_play_probability("delayed", 90, True, 1.0)["play_probability_percent"] < 50


def test_unknown_output_keeps_contract_keys():
    assert set(_unknown(20)) == {"play_probability_percent", "risk_level", "recommendation", "reason"}
