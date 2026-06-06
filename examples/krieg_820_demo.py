import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rainout_agent.agent_response import build_agent_status_response
from rainout_agent.play_probability import calculate_play_probability


def main() -> None:
    estimate = calculate_play_probability(
        official_status="on",
        precipitation_chance_percent=67,
        thunderstorm_likely=True,
        hours_until_game=1.0,
    )
    response = build_agent_status_response(
        field_name="Krieg Field Softball Complex",
        game_time="8:20 PM",
        official_status="on",
        rain_chance_percent=67,
        thunderstorm_likely=True,
        play_probability_percent=estimate["play_probability_percent"],
        rainout_phone="512-978-2680",
    )
    print(json.dumps(response, indent=2))


if __name__ == "__main__":
    main()
