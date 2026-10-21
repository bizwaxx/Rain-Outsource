# Oct 21 retest (2026-10-21, 10:25 AM CT): all fixtures, held out (leave-one-out)

**Step 1 (written first): new caps OFF.** 129 scored fixtures (61 old + 68 new; 3 construction not scored). Every grid axis is re-chosen in each fold with the fixture left out; `threat_cap` and `mx_forecast_cap` fixed off. Confident wrong = p ≥ 80 and not played, or p ≤ 20 and played. The gate (≥70% and 0 confident wrong) was not moved.

| Caps off | All 129 | New 68 only | Old 61 only |
|---|---|---|---|
| Held out, no memory | 79% / 7 | 90% / 3 | 67% / 4 |
| Held out, with drainage memory | 79% / 7 | 88% / 3 | 69% / 4 |
| In-sample (for reference) | 81% / 4 | 90% / 2 | 72% / 2 |

- Confident wrong (no memory): ut-baseball-2023-04-23, freestone-2017-07-01, freestone-2024-05-18, cbb-danlaw-2025-04-22, swan-2021-01-09, fasa-kenedy-2024-07-13, fasa-ingleside-2025-03-29 (the last 3 are in the new 68).
- Memory changes no confident wrong call on this set.
- Scripts: `/workspace/retest/analyze.py` (weather from the `/workspace/wxcache` archive; 18 pre-2016 forecast-archive requests return HTTP 400 and fall back as before).

**Step 2 (caps measured):** see the next section once written.

