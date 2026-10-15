# Play probability

`game_time_play_probability_percent` (in `/v1/status`) and `play_probability_percent`
(in `/v1/schedule/check`) estimate how likely it is that the game is played. This is
not an official decision: the official source always wins. Every result carries a
plain-language `reason`.

## By official status

| Official status | Model |
|---|---|
| `cancelled`, `field_closed` | **0%** |
| `on` | `100 − rain chance × NOAA severity`, −8 for thunderstorms, +5 for 60%+ rain more than 2 h out (unchanged) |
| `unknown` (nothing posted, the common case) | **96% baseline**, lowered by rain and storms (below) |
| `delayed`, or conflicting / unclear signals | at most **50%**: genuinely uncertain |

### Unknown status

Most scheduled games are played, and a silent status page is not a cancellation
signal. A dry forecast therefore sits at **96%**. The missing 4% stands for ordinary
no-shows, forfeits and venue problems.

```
effective_rain = rain_chance × (1 + NOAA severity) / 2      # severity = 1.0 without history or with storms
play = 96 − 0.9 × max(0, effective_rain − 10)
if thunderstorms likely: play = min(play − 8, 50)
```

- Effective rain up to 10% is noise and leaves the odds at 96%.
- Above 10%, the odds fall smoothly, by at most 1 point per point of rain chance,
  with no cliffs.
- NOAA history (see `historical-data.md`) counts at half weight. It softens plain
  rain where rain rarely reaches rainout level, but a very wet forecast still reads
  as a real risk when nobody has confirmed the game. Thunderstorms are never softened.
- Likely thunderstorms (lightning rules) cap the odds at 50%.
- `recommendation` stays `check official source` and `risk_level` stays `unknown`,
  because the status still needs confirming.

Example numbers:

| Forecast | Before | After (no history) | After (Krieg, October NOAA history) | `on` (same history) |
|---|---|---|---|---|
| Dry (5%) | 50 | 96 | 96 | 97 |
| 30% rain | 50 | 78 | 83 | 80 |
| 50% rain | 50 | 60 | 68 | 67 |
| 70% rain | 50 | 42 | 53 | 59 |
| 90% rain | 50 | 24 | 38 | 46 |
| 40% + thunderstorms | 50 | 50 | 50 | 52 |
| 70% + thunderstorms | 50 | 34 | 34 | 27 |

## Schedule check

`/v1/schedule/check` uses the same function, with the real official status (no
longer "assume on"). A game is **at risk** when any of these is true:

- the official status or the calendar says cancelled
- thunderstorms are likely
- the rain chance is 60%+
- play probability is below `risk_threshold` (default 60)

With unknown status and no history, the threshold is crossed at about 50% rain. A
delayed game (50%) is flagged. History-only outlooks beyond the forecast range use
`96% × (1 − share of rainout-level days in that month)`.
