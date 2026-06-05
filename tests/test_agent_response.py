from rainout_agent.agent_response import build_agent_status_response


def test_agent_response_uses_rain_word_and_reports_game_time_probability():
    response = build_agent_status_response(
        field_name="Krieg Field Softball Complex",
        game_time="8:20 PM",
        official_status="on",
        rain_chance_percent=67,
        thunderstorm_likely=True,
        play_probability_percent=25,
        rainout_phone="512-978-2680",
    )

    assert response["field"] == "Krieg Field Softball Complex"
    assert response["game_time"] == "8:20 PM"
    assert response["official_status"] == "on"
    assert response["game_time_play_probability_percent"] == 25
    assert "rain chance" in response["spoken_answer"].lower()
    assert "rainfall" not in response["spoken_answer"].lower()
    assert "512-978-2680" in response["source"]
