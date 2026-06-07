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
    assert havins["official_status_source_url"] == "https://rainoutline.com/search/dnis/5124000060/"


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
        "dripping-springs-tx-ctx-field-of-dreams",
        "dripping-springs-tx-dsysa-baseball-softball-sports-complex",
        "dripping-springs-tx-dsysa-sports-park-fields",
        "san-antonio-tx-lady-bird-johnson-park-softball-fields",
        "san-antonio-tx-mcallister-park-baseball-softball-fields",
        "san-antonio-tx-normoyle-park-baseball-fields",
        "san-antonio-tx-northside-suburban-little-league",
        "helotes-tx-greater-helotes-little-league",
        "san-antonio-tx-seniors-softball-league-normoyle-park",
        "san-antonio-tx-sports-social-club-softball-fields",
        "san-antonio-tx-northwest-little-league",
        "san-antonio-tx-sportskind-softball",
        "houston-tx-houston-sports-social-club-softball",
        "houston-tx-houston-sportsplex",
        "dallas-tx-dallas-sport-social-club-softball",
        "dallas-tx-dfw-adult-baseball-association",
        "fort-worth-tx-fort-worth-university-little-league",
        "fort-worth-tx-senior-softball-dfw",
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
    assert by_id["dripping-springs-tx-ctx-field-of-dreams"]["city"] == "Dripping Springs"
    assert by_id["dripping-springs-tx-dsysa-baseball-softball-sports-complex"]["official_status_source_name"] == "Dripping Springs Youth Sports Association field status page"
    assert by_id["san-antonio-tx-lady-bird-johnson-park-softball-fields"]["city"] == "San Antonio"
    assert by_id["san-antonio-tx-mcallister-park-baseball-softball-fields"]["rainout_phone"] == "210-207-6000"
    assert by_id["san-antonio-tx-northside-suburban-little-league"]["ownership_type"] == "private_nonprofit"
    assert by_id["helotes-tx-greater-helotes-little-league"]["city"] == "Helotes"
    assert by_id["san-antonio-tx-sports-social-club-softball-fields"]["rainout_phone"] == "210-774-4630"
    assert by_id["san-antonio-tx-northwest-little-league"]["official_status_source_name"] == "Northwest Little League official locations page"
    assert by_id["houston-tx-houston-sports-social-club-softball"]["city"] == "Houston"
    assert by_id["dallas-tx-dallas-sport-social-club-softball"]["city"] == "Dallas"
    assert by_id["fort-worth-tx-fort-worth-university-little-league"]["city"] == "Fort Worth"
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


def test_resolve_field_id_accepts_dripping_springs_aliases():
    assert resolve_field_id("Field of Dreams") == "dripping-springs-tx-ctx-field-of-dreams"
    assert resolve_field_id("Dripping Springs Field of Dreams") == "dripping-springs-tx-ctx-field-of-dreams"
    assert resolve_field_id("DSYSA Baseball") == "dripping-springs-tx-dsysa-baseball-softball-sports-complex"
    assert resolve_field_id("Sports Park Fields") == "dripping-springs-tx-dsysa-sports-park-fields"


def test_resolve_field_id_accepts_san_antonio_aliases():
    assert resolve_field_id("Lady Bird Johnson Park") == "san-antonio-tx-lady-bird-johnson-park-softball-fields"
    assert resolve_field_id("McAllister Park") == "san-antonio-tx-mcallister-park-baseball-softball-fields"
    assert resolve_field_id("Normoyle Baseball") == "san-antonio-tx-normoyle-park-baseball-fields"
    assert resolve_field_id("Northside Suburban") == "san-antonio-tx-northside-suburban-little-league"
    assert resolve_field_id("Greater Helotes") == "helotes-tx-greater-helotes-little-league"
    assert resolve_field_id("SASSL") == "san-antonio-tx-seniors-softball-league-normoyle-park"
    assert resolve_field_id("San Antonio SSC") == "san-antonio-tx-sports-social-club-softball-fields"
    assert resolve_field_id("NWLL San Antonio") == "san-antonio-tx-northwest-little-league"
    assert resolve_field_id("Sportskind SA Softball") == "san-antonio-tx-sportskind-softball"


def test_resolve_field_id_accepts_houston_dallas_fort_worth_aliases():
    assert resolve_field_id("Houston SSC") == "houston-tx-houston-sports-social-club-softball"
    assert resolve_field_id("Houston Sportsplex") == "houston-tx-houston-sportsplex"
    assert resolve_field_id("Dallas SSC") == "dallas-tx-dallas-sport-social-club-softball"
    assert resolve_field_id("DFW ABA") == "dallas-tx-dfw-adult-baseball-association"
    assert resolve_field_id("FWULL") == "fort-worth-tx-fort-worth-university-little-league"
    assert resolve_field_id("Senior Softball DFW") == "fort-worth-tx-senior-softball-dfw"


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
    assert result["official_status_source_url"] == "https://rainoutline.com/search/dnis/5124000060/"
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
        "official_status_source_url": "https://rainoutline.com/search/dnis/5124000060/",
        "official_status_source_name": "Austin Parks and Recreation Athletics page",
    }

    result = fetch_official_rainout_status(field)

    assert result["official_status"] == "unknown"
    assert result["source_url"] == "https://rainoutline.com/search/dnis/5124000060/"
    assert result["source_name"] == "Austin Parks and Recreation Athletics page"
    assert result["checked"] is True
    assert "last_checked" in result
    assert requests == [("https://rainoutline.com/search/dnis/5124000060/", 20, "Rainout Source by JEEZ Labs (public pilot)")]


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


def test_san_antonio_private_league_answers_stay_conservative():
    result = build_status_result(
        field_query="San Antonio SSC",
        game_time="2026-06-07T19:00:00-05:00",
        weather={
            "rain_chance_percent": 35,
            "thunderstorm_likely": False,
            "source": "National Weather Service API test data",
            "last_checked": "2026-06-07T16:00:00-05:00",
        },
        official_status="unknown",
    )

    assert result["field_id"] == "san-antonio-tx-sports-social-club-softball-fields"
    assert result["official_status_source_url"] == "https://sanantoniossc.com/alerts"
    assert "official rainout status is unknown for san antonio sports and social club softball fields" in result["spoken_answer"].lower()
    assert "210-774-4630" in result["spoken_answer"]
    assert "rainfall" not in result["spoken_answer"].lower()


def test_houston_dallas_fort_worth_answers_stay_conservative():
    for query, field_id in [
        ("Houston SSC", "houston-tx-houston-sports-social-club-softball"),
        ("Dallas SSC", "dallas-tx-dallas-sport-social-club-softball"),
        ("FWULL", "fort-worth-tx-fort-worth-university-little-league"),
    ]:
        result = build_status_result(
            field_query=query,
            game_time="2026-06-07T19:00:00-05:00",
            weather={
                "rain_chance_percent": 40,
                "thunderstorm_likely": False,
                "source": "National Weather Service API test data",
                "last_checked": "2026-06-07T16:00:00-05:00",
            },
            official_status="unknown",
        )

        assert result["field_id"] == field_id
        assert "official rainout status is unknown" in result["spoken_answer"].lower()
        assert "check the official source" in result["spoken_answer"].lower()
        assert "rainfall" not in result["spoken_answer"].lower()
