#!/usr/bin/env python3
"""TeamSnap prototype CLI: a team's games with official status first and estimated play odds.

    python scripts/teamsnap_odds.py                       # fixtures, demo weather (no network)
    python scripts/teamsnap_odds.py --live-weather --rebase  # fixtures, real NWS forecast, dates moved to now
    TEAMSNAP_TOKEN=... python scripts/teamsnap_odds.py --team-id 123 --live-weather

The real TeamSnap API is used only when TEAMSNAP_TOKEN is set. The token is never printed.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rainout_agent import teamsnap  # noqa: E402

FIXTURE_TEAM = "9000001"
FIXTURE_NOW = datetime(2026, 10, 30, 15, 0, tzinfo=timezone.utc)  # 10 AM CT, day before the first fixture game


def demo_forecast(rain_by_field: dict[str, int]):
    """Fixed fake forecast so the demo runs offline. Clearly NOT real weather."""
    def provider(field, timeout):
        base = FIXTURE_NOW.replace(minute=0)
        rain = rain_by_field.get(field["id"], 15)
        return {"source": "demo forecast (fixture, not real weather)", "source_id": "demo", "periods": [
            {"startTime": (base + timedelta(hours=h)).isoformat(), "endTime": (base + timedelta(hours=h + 1)).isoformat(),
             "probabilityOfPrecipitation": {"value": rain}, "shortForecast": "Rain" if rain >= 60 else "Partly Cloudy"}
            for h in range(156)]}
    return provider


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--team-id", default=FIXTURE_TEAM)
    ap.add_argument("--live-weather", action="store_true", help="use the real NWS forecast and official sources")
    ap.add_argument("--rebase", action="store_true", help="move fixture dates so the first game is ~1 day from now")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    source = teamsnap.default_source()
    kwargs = {}
    now = None
    shift = None
    if source.name == "fixtures":
        if args.rebase:
            now = datetime.now(timezone.utc)
            shift = timedelta(days=round((now - FIXTURE_NOW).total_seconds() / 86400))  # whole days keep game times
        else:
            now = FIXTURE_NOW
    if not args.live_weather:
        now = now or datetime.now(timezone.utc)
        kwargs = {"forecast_provider": demo_forecast({"austin-tx-krieg-field-softball-complex": 70}),
                  "official_provider": lambda field, timeout: {"official_status": "unknown", "checked": True}}
    result = teamsnap.team_odds(args.team_id, source=source, now=now, shift=shift, **kwargs)
    if not args.live_weather:
        result["warnings"].append("Demo forecast used (not real weather). Pass --live-weather for real NWS data.")
    print(json.dumps(result, indent=2, default=str) if args.json else teamsnap.format_text(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
