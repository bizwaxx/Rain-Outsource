def calculate_play_probability(
    official_status: str,
    precipitation_chance_percent: int | float,
    thunderstorm_likely: bool,
    hours_until_game: float,
) -> dict:
    """Estimate whether a game will be playable at game time.

    This is not an official cancellation decision. The official source wins.
    The score helps a parent decide whether the drive is risky.
    """
    status = (official_status or "unknown").strip().lower()
    rain_chance = max(0, min(100, int(round(precipitation_chance_percent))))

    if status in {"cancelled", "canceled", "field closed", "field_closed", "closed"}:
        return {
            "play_probability_percent": 0,
            "risk_level": "officially cancelled",
            "recommendation": "do not drive",
            "reason": "Official status is cancelled or the field is closed.",
        }

    if status not in {"on", "playing", "open"}:
        return {
            "play_probability_percent": 50,
            "risk_level": "unknown",
            "recommendation": "check official source",
            "reason": "Official status is unknown, so weather alone is not enough to decide.",
        }

    probability = 100 - rain_chance
    if thunderstorm_likely:
        probability -= 8
    if hours_until_game <= 1 and rain_chance >= 60:
        probability -= 0
    elif hours_until_game > 2 and rain_chance >= 60:
        probability += 5

    probability = max(0, min(100, probability))

    if rain_chance >= 60 or thunderstorm_likely:
        risk_level = "high"
        recommendation = "check again before leaving"
        reason = (
            f"Official status is on, but rain chance is {rain_chance}%"
            + (" with thunderstorms likely." if thunderstorm_likely else ".")
        )
    elif rain_chance >= 35:
        risk_level = "medium"
        recommendation = "check again before leaving"
        reason = f"Official status is on, with moderate rain chance at {rain_chance}%."
    else:
        risk_level = "low"
        recommendation = "likely safe to drive"
        reason = f"Official status is on, with low rain chance at {rain_chance}%."

    return {
        "play_probability_percent": probability,
        "risk_level": risk_level,
        "recommendation": recommendation,
        "reason": reason,
    }
