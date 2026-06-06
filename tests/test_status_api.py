from rainout_agent.status_api import build_status_result, list_supported_fields, resolve_field_id


def test_resolve_field_id_accepts_krieg_aliases():
    assert resolve_field_id("Krieg") == "austin-tx-krieg-field-softball-complex"
    assert resolve_field_id("krieg field") == "austin-tx-krieg-field-softball-complex"
    assert resolve_field_id("austin-tx-krieg-field-softball-complex") == "austin-tx-krieg-field-softball-complex"


def test_resolve_field_id_accepts_havins_aliases():
    assert resolve_field_id("Havins") == "austin-tx-havins-softball-complex"
    assert resolve_field_id("havins complex") == "austin-tx-havins-softball-complex"
    assert resolve_field_id("austin-tx-havins-softball-complex") == "austin-tx-havins-softball-complex"


def test_list_supported_fields_includes_krieg_and_havins():
    fields = list_supported_fields()

    field_ids = {field["field_id"] for field in fields}
    assert "austin-tx-krieg-field-softball-complex" in field_ids
    assert "austin-tx-havins-softball-complex" in field_ids
    havins = next(field for field in fields if field["field_id"] == "austin-tx-havins-softball-complex")
    assert havins["name"] == "Havins Softball Complex"
    assert havins["official_status_source_url"] == "https://www.austintexas.gov/department/athletics"


def test_build_status_result_uses_live_weather_inputs_and_voice_safe_answer():
    result = build_status_result(
        field_query="Krieg",
        game_time="2026-06-05T20:20:00-05:00",
        weather={
            "rain_chance_percent": 67,
            "thunderstorm_likely": True,
            "source": "National Weather Service API test data",
            "last_checked": "2026-06-05T19:00:00-05:00",
        },
        official_status="unknown",
    )

    assert result["creator"] == "JEEZ Labs"
    assert result["field_id"] == "austin-tx-krieg-field-softball-complex"
    assert result["field"] == "Krieg Field Softball Complex"
    assert result["official_status"] == "unknown"
    assert result["rain_chance_percent"] == 67
    assert result["thunderstorm_likely"] is True
    assert result["game_time_play_probability_percent"] == 50
    assert result["recommendation"] == "check official source"
    assert "official rainout status is unknown" in result["spoken_answer"].lower()
    assert "rain chance" in result["spoken_answer"].lower()
    assert "rainfall" not in result["spoken_answer"].lower()
    assert "last_checked" in result


def test_build_status_result_supports_havins_with_same_voice_safety_rule():
    result = build_status_result(
        field_query="Havins",
        game_time="2026-06-06T20:20:00-05:00",
        weather={
            "rain_chance_percent": 35,
            "thunderstorm_likely": False,
            "source": "National Weather Service API test data",
            "last_checked": "2026-06-06T19:00:00-05:00",
        },
        official_status="unknown",
    )

    assert result["field_id"] == "austin-tx-havins-softball-complex"
    assert result["field"] == "Havins Softball Complex"
    assert result["address"] == "12138 N Lamar Blvd, Austin, TX 78753"
    assert result["official_status_source_url"] == "https://www.austintexas.gov/department/athletics"
    assert result["rain_chance_percent"] == 35
    assert "official rainout status is unknown" in result["spoken_answer"].lower()
    assert "rain chance" in result["spoken_answer"].lower()


def test_build_status_result_returns_error_for_unknown_field():
    result = build_status_result(
        field_query="Not A Field",
        game_time="2026-06-05T20:20:00-05:00",
        weather={"rain_chance_percent": 0, "thunderstorm_likely": False, "source": "test"},
    )

    assert result["error"] == "unknown_field"
    assert "Krieg" in result["spoken_answer"]
