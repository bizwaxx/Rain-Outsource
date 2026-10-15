from rainout_agent.historical import rain_severity

UNKNOWN_BASELINE_PERCENT = 96
UNKNOWN_RAIN_NOISE_PERCENT = 10
UNKNOWN_RAIN_SLOPE = 0.9
UNKNOWN_STORM_CAP_PERCENT = 50
UNCERTAIN_STATUS_CAP_PERCENT = 50
UNCERTAIN_STATUSES = {"delayed", "postponed", "conflicting", "uncertain", "mixed"}

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

    Official status:
    * cancelled / closed: 0%.
    * on: 100% minus the (calibrated) rain chance, as before.
    * unknown (nothing posted): starts at a 96% baseline and is lowered by rain
      and storms (see ``unknown_status_play_percent``), because most scheduled
      games are played and silence is not a cancellation signal.
    * delayed or conflicting signals: capped at 50%.
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

    if status in UNCERTAIN_STATUSES:
        return _uncertain_status_probability(status, rain_chance, thunderstorm_likely, historical_calibration)

    if status not in {"on", "playing", "open"}:
        return _unknown_status_probability(rain_chance, thunderstorm_likely, historical_calibration)

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
        result["reason"] = f"{reason} {_history_sentence(historical_calibration, cancel_given_wet)}"
        result["rain_severity"] = severity
    return result


def _history_sentence(historical_calibration: dict, cancel_given_wet: float) -> str:
    month = historical_calibration.get("month")
    month_name = MONTH_NAMES[int(month) - 1] if month and str(month).isdigit() and 1 <= int(month) <= 12 else "this month"
    threshold = historical_calibration.get("rain_to_cancel_threshold_in")
    level = f"{threshold} inch" if threshold else "rainout-level"
    return (
        f"Historically about {round(cancel_given_wet * 100)}% of rainy {month_name} days near this field "
        f"reach {level} rain totals (NOAA records), which is factored into the play probability."
    )


def _rain_factor(rain_chance: int, thunderstorm_likely: bool, historical_calibration: dict | None) -> tuple[float, float, float | None]:
    """Rain chance weighted by how often rain here reaches rainout level (NOAA)."""
    severity = 1.0
    cancel_given_wet = None
    if historical_calibration and historical_calibration.get("cancel_given_wet") is not None:
        cancel_given_wet = float(historical_calibration["cancel_given_wet"])
        severity = rain_severity(cancel_given_wet, thunderstorm_likely)
    return rain_chance * severity, severity, cancel_given_wet


def unknown_status_play_percent(rain_chance: int, thunderstorm_likely: bool, effective_rain: float) -> int:
    """Play odds when nobody has posted an official status yet.

    Most scheduled games are played, so a dry forecast sits at the 96% baseline
    (the 4% covers ordinary no-shows, forfeits and venue problems). Effective
    rain chance up to 10% is treated as noise. Above that, each point lowers the
    odds by 0.9, so 30% rain gives 78%, 50% gives 60% and 70% gives 42%
    (before NOAA calibration).
    Likely thunderstorms (lightning rules) take 8 more points off and cap the
    odds at 50%.
    """
    probability = UNKNOWN_BASELINE_PERCENT - UNKNOWN_RAIN_SLOPE * max(0.0, effective_rain - UNKNOWN_RAIN_NOISE_PERCENT)
    if thunderstorm_likely:
        probability = min(probability - 8, UNKNOWN_STORM_CAP_PERCENT)
    return int(max(0, min(UNKNOWN_BASELINE_PERCENT, round(probability))))


def _unknown_calibrated_rain(rain_chance: int, thunderstorm_likely: bool, historical_calibration: dict | None) -> tuple[float, float, float | None]:
    """NOAA history counts at half weight here, so it softens plain rain without
    letting a very wet forecast look safe when nobody has confirmed the game."""
    _, severity, cancel_given_wet = _rain_factor(rain_chance, thunderstorm_likely, historical_calibration)
    factor = round((1.0 + severity) / 2, 3) if cancel_given_wet is not None else 1.0
    return rain_chance * factor, factor, cancel_given_wet


def _unknown_status_probability(rain_chance: int, thunderstorm_likely: bool, historical_calibration: dict | None) -> dict:
    effective, severity, cancel_given_wet = _unknown_calibrated_rain(rain_chance, thunderstorm_likely, historical_calibration)
    probability = unknown_status_play_percent(rain_chance, thunderstorm_likely, effective)
    if thunderstorm_likely:
        reason = (
            f"Official status has not been posted. Thunderstorms are likely and rain chance is {rain_chance}%, "
            "so lightning delays or a rainout are a real possibility."
        )
    elif effective <= UNKNOWN_RAIN_NOISE_PERCENT:
        reason = (
            f"Official status has not been posted, but rain is not a factor (rain chance {rain_chance}%). "
            "Games like this are almost always played; the small remaining risk is ordinary no-shows or venue issues."
        )
    else:
        reason = (
            f"Official status has not been posted. Rain chance is {rain_chance}%, "
            + ("which makes a rainout a real possibility." if probability <= 60 else "which lowers the play odds somewhat.")
        )
    reason += " Confirm with the official source before leaving."
    result = {
        "play_probability_percent": probability,
        "risk_level": "unknown",
        "recommendation": "check official source",
        "reason": reason,
    }
    if cancel_given_wet is not None:
        result["reason"] = f"{reason} {_history_sentence(historical_calibration, cancel_given_wet)}"
        result["rain_severity"] = severity
    return result


def _uncertain_status_probability(status: str, rain_chance: int, thunderstorm_likely: bool, historical_calibration: dict | None) -> dict:
    """Delayed or conflicting official signals: genuinely a coin flip at best."""
    effective, _, _ = _unknown_calibrated_rain(rain_chance, thunderstorm_likely, historical_calibration)
    probability = min(UNCERTAIN_STATUS_CAP_PERCENT, unknown_status_play_percent(rain_chance, thunderstorm_likely, effective))
    label = "delayed" if status in {"delayed", "postponed"} else "unclear (sources disagree)"
    return {
        "play_probability_percent": probability,
        "risk_level": "unknown",
        "recommendation": "check official source",
        "reason": f"Official status is {label}, so whether the game is played is genuinely uncertain. Check the official source before leaving.",
    }
