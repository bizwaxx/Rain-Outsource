def build_agent_status_response(
    field_name: str,
    game_time: str,
    official_status: str,
    rain_chance_percent: int,
    thunderstorm_likely: bool,
    play_probability_percent: int,
    rainout_phone: str,
) -> dict:
    """Build an agent-readable and voice-friendly game status response."""
    official = (official_status or "unknown").strip().lower()
    storm_phrase = " with storms likely" if thunderstorm_likely else ""

    if official == "on":
        spoken = (
            f"Official status for {field_name} is on. "
            f"At game time, {game_time}, rain chance is {rain_chance_percent}%{storm_phrase}. "
            f"Estimated play probability is {play_probability_percent}%. "
            "Check the rainout line again right before leaving."
        )
    elif official in {"cancelled", "canceled", "field closed", "closed"}:
        spoken = (
            f"Official status for {field_name} is cancelled or closed. "
            "Do not drive unless the official source changes."
        )
    else:
        spoken = (
            f"Official status for {field_name} is unknown. "
            f"Rain chance at {game_time} is {rain_chance_percent}%{storm_phrase}. "
            "Check the official rainout line before leaving."
        )

    return {
        "creator": "JEEZ Labs",
        "field": field_name,
        "game_time": game_time,
        "official_status": official,
        "rain_chance_percent": rain_chance_percent,
        "thunderstorm_likely": thunderstorm_likely,
        "game_time_play_probability_percent": play_probability_percent,
        "source": f"Rainout phone: {rainout_phone}; weather: National Weather Service",
        "spoken_answer": spoken,
    }
