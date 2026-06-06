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


def test_unknown_official_status_caps_confidence_even_with_good_weather():
    result = calculate_play_probability(
        official_status="unknown",
        precipitation_chance_percent=10,
        thunderstorm_likely=False,
        hours_until_game=2.0,
    )

    assert result["play_probability_percent"] == 50
    assert result["risk_level"] == "unknown"
    assert result["recommendation"] == "check official source"
