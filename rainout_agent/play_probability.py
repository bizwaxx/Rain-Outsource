from rainout_agent.historical import rain_severity

MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def calculate_play_probability(
    official_status: str,
    precipitation_chance_percent: int | float,
    thunderstorm_likely: bool,
    hours_until_game: float,
    historical_calibration: dict | None = None,
) -> dict:
    """Estimate whether a game will be playable at game time.

    This is not an official cancellation decision. The official source wins.
    The score helps a parent decide whether the drive is risky.

    ``historical_calibration`` (optional) comes from
    ``rainout_agent.historical.calibration_for`` and carries the field's NOAA
    climatology for the game month. Without it, the rain chance counts fully
    against play (the original model). With it, only the share of rain that
    historically reaches the field's rain-to-cancel level counts, blended with
    a 50% floor; thunderstorms always count fully.
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

    severity = 1.0
    cancel_given_wet = None
    if historical_calibration and historical_calibration.get("cancel_given_wet") is not None:
        cancel_given_wet = float(historical_calibration["cancel_given_wet"])
        severity = rain_severity(cancel_given_wet, thunderstorm_likely)

    probability = 100 - int(round(rain_chance * severity))
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

    result = {
        "play_probability_percent": probability,
        "risk_level": risk_level,
        "recommendation": recommendation,
        "reason": reason,
    }
    if cancel_given_wet is not None:
        month = historical_calibration.get("month")
        month_name = MONTH_NAMES[int(month) - 1] if month and str(month).isdigit() and 1 <= int(month) <= 12 else "this month"
        threshold = historical_calibration.get("rain_to_cancel_threshold_in")
        level = f"{threshold} inch" if threshold else "rainout-level"
        result["reason"] = (
            f"{reason} Historically about {round(cancel_given_wet * 100)}% of rainy {month_name} days near this field "
            f"reach {level} rain totals (NOAA records), which is factored into the play probability."
        )
        result["rain_severity"] = severity
    return result
