from rainout_agent.status_api import (
    build_status_result,
    fetch_official_rainout_status,
    list_supported_fields,
    parse_official_rainout_status,
    resolve_field_id,
)


def test_resolve_field_id_accepts_krieg_aliases():
    assert resolve_field_id("Krieg") == "austin-tx-krieg-field-softball-complex"
    assert resolve_field_id("krieg field") == "austin-tx-krieg-field-softball-complex"
    assert resolve_field_id("austin-tx-krieg-field-softball-complex") == "austin-tx-krieg-field-softball-complex"


def test_resolve_field_id_accepts_havins_aliases():
    assert resolve_field_id("Havins") == "austin-tx-havins-softball-complex"
    assert resolve_field_id("havins complex") == "austin-tx-havins-softball-complex"
    assert resolve_field_id("austin-tx-havins-softball-complex") == "austin-tx-havins-softball-complex"


def test_resolve_field_id_accepts_craig_as_voice_alias_for_krieg():
    assert resolve_field_id("Craig") == "austin-tx-krieg-field-softball-complex"
    assert resolve_field_id("Craig Field") == "austin-tx-krieg-field-softball-complex"


def test_list_supported_fields_includes_krieg_and_havins():
    fields = list_supported_fields()

    field_ids = {field["field_id"] for field in fields}
    assert "austin-tx-krieg-field-softball-complex" in field_ids
    assert "austin-tx-havins-softball-complex" in field_ids
    havins = next(field for field in fields if field["field_id"] == "austin-tx-havins-softball-complex")
    assert havins["name"] == "Havins Softball Complex"
    assert havins["official_status_source_url"] == "https://www.austintexas.gov/department/athletics"


def test_list_supported_fields_includes_austin_public_and_private_expansion_batch():
    fields = list_supported_fields()

    by_id = {field["field_id"]: field for field in fields}
    expected = {
        "austin-metro-northeast-metropolitan-park",
        "austin-metro-southeast-metropolitan-park",
        "austin-tx-oak-hill-youth-sports-association",
        "manchaca-tx-manchaca-optimist-youth-sports-complex",
        "austin-tx-town-and-country-sports-complex",
        "austin-tx-balcones-youth-sports",
        "austin-tx-northwest-little-league",
        "austin-tx-western-hills-little-league",
        "round-rock-tx-old-settlers-baseball-complex",
        "round-rock-tx-old-settlers-softball-complex",
        "cedar-park-tx-brushy-creek-sports-park-softball-fields",
        "cedar-park-tx-elizabeth-milburn-park-multipurpose-fields",
        "austin-tx-downs-field-ambl",
        "austin-tx-anderson-high-school-baseball-field-ambl",
        "georgetown-tx-san-gabriel-park-gyba-field-8-ambl",
    }
    assert expected.issubset(by_id)
    assert by_id["austin-metro-northeast-metropolitan-park"]["ownership_type"] == "public"
    assert by_id["austin-tx-oak-hill-youth-sports-association"]["ownership_type"] == "private_nonprofit"
    assert by_id["austin-tx-town-and-country-sports-complex"]["status_source_type"] == "official_field_status_page"
    assert by_id["austin-tx-balcones-youth-sports"]["ownership_type"] == "private_nonprofit"
    assert by_id["round-rock-tx-old-settlers-baseball-complex"]["rainout_phone"] == "512-218-5540"
    assert by_id["cedar-park-tx-brushy-creek-sports-park-softball-fields"]["status_source_type"] == "official_field_status_page"
    assert by_id["austin-tx-downs-field-ambl"]["official_status_source_name"] == "Austin Metro Baseball League fields page"
    assert by_id["georgetown-tx-san-gabriel-park-gyba-field-8-ambl"]["city"] == "Georgetown"
    assert "official_status_source_url" in by_id["manchaca-tx-manchaca-optimist-youth-sports-complex"]


def test_resolve_field_id_accepts_new_private_field_aliases():
    assert resolve_field_id("T&C Sports") == "austin-tx-town-and-country-sports-complex"
    assert resolve_field_id("Balcones") == "austin-tx-balcones-youth-sports"
    assert resolve_field_id("NWLL") == "austin-tx-northwest-little-league"
    assert resolve_field_id("Western Hills") == "austin-tx-western-hills-little-league"


def test_resolve_field_id_accepts_round_rock_and_cedar_park_aliases():
    assert resolve_field_id("Old Settlers Baseball") == "round-rock-tx-old-settlers-baseball-complex"
    assert resolve_field_id("OSP Softball") == "round-rock-tx-old-settlers-softball-complex"
    assert resolve_field_id("Brushy Creek Softball") == "cedar-park-tx-brushy-creek-sports-park-softball-fields"
    assert resolve_field_id("Milburn Park") == "cedar-park-tx-elizabeth-milburn-park-multipurpose-fields"


def test_resolve_field_id_accepts_austin_metro_baseball_league_aliases():
    assert resolve_field_id("Downs") == "austin-tx-downs-field-ambl"
    assert resolve_field_id("Downs Field") == "austin-tx-downs-field-ambl"
    assert resolve_field_id("Anderson High School") == "austin-tx-anderson-high-school-baseball-field-ambl"
    assert resolve_field_id("Georgetown Fields") == "georgetown-tx-san-gabriel-park-gyba-field-8-ambl"
    assert resolve_field_id("GYBA Field 8") == "georgetown-tx-san-gabriel-park-gyba-field-8-ambl"


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
    assert result["answer_requirements"]["must_include"] == [
        "field name",
        "official status",
        "rain chance",
        "play probability",
        "rainout phone or official source",
    ]
    assert result["answer_requirements"]["if_official_status_unknown"] == "Say official status is unknown and tell the user to call 512-978-2680 before leaving."


def test_build_status_result_for_field_without_phone_points_to_official_source():
    result = build_status_result(
        field_query="Oak Hill Youth Sports",
        game_time="2026-06-06T20:20:00-05:00",
        weather={
            "rain_chance_percent": 20,
            "thunderstorm_likely": False,
            "source": "National Weather Service API test data",
            "last_checked": "2026-06-06T19:00:00-05:00",
        },
        official_status="unknown",
    )

    assert result["field"] == "Oak Hill Youth Sports Association"
    assert result["official_status"] == "unknown"
    assert "official source" in result["spoken_answer"].lower()
    assert "not published" not in result["spoken_answer"].lower()
    assert result["answer_requirements"]["if_official_status_unknown"] == "Say official status is unknown and tell the user to check the official source before leaving."


def test_build_status_result_returns_error_for_unknown_field():
    result = build_status_result(
        field_query="Not A Field",
        game_time="2026-06-05T20:20:00-05:00",
        weather={"rain_chance_percent": 0, "thunderstorm_likely": False, "source": "test"},
    )

    assert result["error"] == "unknown_field"
    assert "Krieg" in result["spoken_answer"]


def test_parse_official_rainout_status_is_conservative():
    assert parse_official_rainout_status("All adult softball games are cancelled tonight") == "cancelled"
    assert parse_official_rainout_status("Fields are closed due to wet conditions") == "field_closed"
    assert parse_official_rainout_status("Games delayed 30 minutes for lightning") == "delayed"
    assert parse_official_rainout_status("Registration is open for summer softball") == "unknown"


def test_parse_official_rainout_status_handles_private_field_status_widgets():
    assert parse_official_rainout_status('<li class="skItem skOpen"><p>Northwest Little League</p></li>') == "on"
    assert parse_official_rainout_status('<li class="skItem skClose"><p>Balcones Youth Sports</p></li>') == "field_closed"
    assert parse_official_rainout_status("Baseball: Fields 1-9 Open. Subject to closure at time of play.") == "on"
    assert parse_official_rainout_status("Softball: All diamonds Closed due to weather.") == "field_closed"
    assert parse_official_rainout_status("Brushy Creek Sports Park Softball Fields: CLOSED") == "field_closed"


def test_fetch_official_rainout_status_checks_source_and_keeps_unknown_without_clear_signal(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b"<html><body>Austin Parks Athletics registration is open.</body></html>"

    requests = []

    def fake_urlopen(request, timeout):
        requests.append((request.full_url, timeout, request.headers.get("User-agent")))
        return FakeResponse()

    monkeypatch.setattr("rainout_agent.status_api.urllib.request.urlopen", fake_urlopen)
    field = {
        "official_status_source_url": "https://www.austintexas.gov/department/athletics",
        "official_status_source_name": "Austin Parks and Recreation Athletics page",
    }

    result = fetch_official_rainout_status(field)

    assert result["official_status"] == "unknown"
    assert result["source_url"] == "https://www.austintexas.gov/department/athletics"
    assert result["source_name"] == "Austin Parks and Recreation Athletics page"
    assert result["checked"] is True
    assert "last_checked" in result
    assert requests == [("https://www.austintexas.gov/department/athletics", 20, "Rainout Source by JEEZ Labs (public pilot)")]


def test_fetch_official_rainout_status_stays_unknown_when_source_unavailable(monkeypatch):
    def fake_urlopen(request, timeout):
        raise OSError("network blocked")

    monkeypatch.setattr("rainout_agent.status_api.urllib.request.urlopen", fake_urlopen)
    field = {
        "official_status_source_url": "https://example.invalid/status",
        "official_status_source_name": "Example status page",
    }

    result = fetch_official_rainout_status(field)

    assert result["official_status"] == "unknown"
    assert result["checked"] is False
    assert result["error"] == "official_source_unavailable"


def test_fetch_official_rainout_status_does_not_poll_directory_pages(monkeypatch):
    def fake_urlopen(request, timeout):
        raise AssertionError("directory pages should not be polled as rainout feeds")

    monkeypatch.setattr("rainout_agent.status_api.urllib.request.urlopen", fake_urlopen)
    field = {
        "official_status_source_url": "https://parks.example.test/venue",
        "official_status_source_name": "Venue information page",
        "poll_official_status": False,
    }

    result = fetch_official_rainout_status(field)

    assert result["official_status"] == "unknown"
    assert result["checked"] is False
    assert result["polling_skipped"] == "not_a_status_feed"


def test_build_status_result_uses_fetched_official_status_when_not_provided(monkeypatch):
    monkeypatch.setattr(
        "rainout_agent.status_api.fetch_official_rainout_status",
        lambda field: {
            "official_status": "cancelled",
            "source_url": field["official_status_source_url"],
            "source_name": field["official_status_source_name"],
            "checked": True,
            "last_checked": "2026-06-06T19:10:00+00:00",
        },
    )

    result = build_status_result(
        field_query="Krieg",
        game_time="2026-06-06T20:20:00-05:00",
        weather={
            "rain_chance_percent": 5,
            "thunderstorm_likely": False,
            "source": "National Weather Service API test data",
            "last_checked": "2026-06-06T19:00:00-05:00",
        },
    )

    assert result["official_status"] == "cancelled"
    assert result["official_status_checked"] is True
    assert result["official_status_last_checked"] == "2026-06-06T19:10:00+00:00"
    assert result["recommendation"] == "do not drive"
