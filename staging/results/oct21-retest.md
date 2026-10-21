# Oct 21 retest (2026-10-21, 10:25 AM to 2:40 PM CT): all fixtures, held out (leave-one-out)

**Step 1 (written first): new caps OFF.** 129 scored fixtures (61 old + 68 new; 3 construction not scored). Every grid axis is re-chosen in each fold with the fixture left out; `threat_cap` and `mx_forecast_cap` fixed off. Confident wrong = p ≥ 80 and not played, or p ≤ 20 and played. The gate (≥70% and 0 confident wrong) was not moved.

| Caps off | All 129 | New 68 only | Old 61 only |
|---|---|---|---|
| Held out, no memory | 79% / 7 | 90% / 3 | 67% / 4 |
| Held out, with drainage memory | 79% / 7 | 88% / 3 | 69% / 4 |
| In-sample (for reference) | 81% / 4 | 90% / 2 | 72% / 2 |

- Confident wrong (no memory): ut-baseball-2023-04-23, freestone-2017-07-01, freestone-2024-05-18, cbb-danlaw-2025-04-22, swan-2021-01-09, fasa-kenedy-2024-07-13, fasa-ingleside-2025-03-29 (the last 3 are in the new 68).
- Memory changes no confident wrong call on this set.
- Scripts: `/workspace/retest/analyze.py` (weather from the `/workspace/wxcache` archive; 18 pre-2016 forecast-archive requests return HTTP 400 and fall back as before).

## Step 2 (caps measured), Oct 21, 1:35 PM CT

Each row fixes the caps as shown and re-chooses every other axis in each held-out fold (1,327,104 grid points, run through the committed code). Shown as directional % / confident wrong. The gate (≥70% and 0 confident wrong) was not moved.

| Caps | All 129 | New 68 | Old 61 | All 129 with memory |
|---|---|---|---|---|
| All caps off | 79% / 7 | 90% / 3 | 67% / 4 | 79% / 7 |
| Threat cap only (previous default, 94986b3) | 80% / 4 | 88% / 1 | 70% / 3 | 80% / 4 |
| Threat + unstable-air cap | 82% / 3 | 90% / 1 | 74% / 2 | 82% / 3 |
| Threat + cancel floor | 81% / 3 | 91% / 0 | 70% / 3 | 81% / 3 |
| **Threat + unstable-air + cancel floor (new default)** | **84% / 2** | **93% / 0** | **74% / 2** | **83% / 2** |
| Same + motocross forecast cap | 84% / 2 | 93% / 0 | 74% / 2 | 83% / 2 |
| Every cap axis re-chosen per fold | 84% / 4 | 93% / 1 | 74% / 3 | 83% / 4 |

In-sample with the new defaults: 84% / 1 (only ut-baseball-2023-04-23).

**On by default now (commit a6ae790):**
- **Threat cap (79%)**: a storm or rain threat the day before or day of means never "confident play".
- **Unstable-air cap (new)**: CAPE ≥ 2500 in the window or that day means not above 79%. Note: "Unstable air: pop-up storms can form even with a low rain chance, and organizers may call it early." It affects 6 played fixtures, which drop from confident to 79%. The result is the same at 2000 and 2500.
- **Cancel floor (new, 21%)**: weather alone never says "confident cancel". Note: "Weather alone can't make a cancellation certain (organizers sometimes play through a bad forecast); check the official status." A posted official cancellation still gives 0%, because this model only runs when no status is posted.
- **Motocross note**: always shown with a threat ("Organizers here sometimes cancel on the forecast alone…"). The motocross cap stays off because it adds nothing over the threat cap.
- **Other recalibrated axes**: slow drainage 1.2 (was 0.8), soak weights 1 / 0.6 / 0.3 (was 1 / 0.3 / 0.15), maintained-venue soak 0.2 (was 0.35).
- **Cost**: band accuracy goes from 70% to 69%.

**Remaining held-out confident wrong (2), so the gate still fails:**
- **ut-baseball-2023-04-23** (96%, not played): **weather-unpredictable**. There was no threat signal at all (5% chance, no storms, no CAPE).
- **cbb-bobcat-2019-02-19** (held out 96%, in-sample 79%, not played): **fixable but unstable**. It was a cold, wet February day (43°F, 0.19 in that morning on turf). It is only capped when its own fold picks the 0.15 in humility threshold, so a "cold and wet" signal (about 45°F with rain that morning) might fix it. That rule rests on one fixture, so I didn't add it.

**Fixed versus caps off:** freestone-2017-07-01 and freestone-2024-05-18 (forecast cancels), swan-2021-01-09 (cold), fasa-ingleside-2025-03-29, cbb-danlaw-2025-04-22 (unstable air at a 23% chance), and fasa-kenedy-2024-07-13 (played through a 90% chance, now floored at 21%).

**Caveats:** the cap settings were picked by looking at held-out numbers across 6 options, so 84% is slightly optimistic. The FASA "played" labels cover whole weekends.

## Step 3 (push for 0 confident wrong), Oct 21, 2:40 PM CT

**Short answer: 0 confident wrong is reachable only with a blanket ceiling, and the ceiling is not honest calibration, so it is built but left OFF for Jimmy to decide.** The gate (≥70% and 0 confident wrong) was not moved. With the ceiling off, the gate still fails on 1 to 2 events.

**New data:** 25 cold, wet early-season college games (18 baseball, 7 softball). They were picked on ERA5 weather *before* looking at results: ≤55°F at 6 PM and ≥0.10 in of rain in the prior 24 h, Feb 10 to Mar 31, 2017 to 2025, at the fixture ballparks. Every ESPN-listed home game with a checkable venue was kept. 22 were played and 3 were not (all in the Feb 2021 ice storm, with team or opponent sources). 24 exclusions are listed with reasons. Total: 154 scored.

Held out, caps fixed, other axes re-chosen per fold. Shown as directional % / confident wrong:

| Option | All 154 | Old 61 | Oct 21 68 | Cold-wet 25 | Answers at 80%+ |
|---|---|---|---|---|---|
| Current defaults (before this step) | 85% / 2 | 74% / 2 | 91% / 0 | 96% / 0 | 39 (37 right) |
| + cold-wet cap (≤45°F and ≥0.1 in) | 85% / 1 | 74% / 1 | 91% / 0 | 96% / 0 | 49 |
| + cold-wet cap (≤50°F and ≥0.1 in) | 85% / 1 | 74% / 1 | 91% / 0 | 96% / 0 | 45 |
| + status ceiling 79 | **85% / 0** | 74% / 0 | 91% / 0 | 96% / 0 | **0** |
| + both | 85% / 0 | 74% / 0 | 91% / 0 | 96% / 0 | 0 |

On the 129 set before the new games, the same options gave: defaults 84% / 2, cold-wet 84% / 1, and ceiling 83% / 0. Accuracy never fell below 70% on any slice except the old 61, which stays at 74%.

**(b) Cold-and-wet rule: did not hold up, so not adopted.**
- **What it fixes:** it only fixes cbb-bobcat-2019-02-19.
- **What the fresh data shows:** 19 of the 25 new games were 36 to 50°F with rain that morning or the day before, and **16 of 16 above 33°F were played**. The only cold-wet cancels were the ≤33°F ice-storm days, which the existing cold rule (≤35°F) already handles.
- **Why not adopt it:** a "cold and wet means maybe cancelled" rule rests on one game, and the fresh data contradicts it.

**(a) Status ceiling ("No official status yet; organizers can still cancel for non-weather reasons."): works, but it is a policy choice, not a fix.**
- **What it does:** it clears both remaining confident wrong calls with no loss in accuracy (85%) or band accuracy (71%).
- **Is it honest?** Held out, the model's 80%+ answers were right **37 of 39 times (95%)**, and its 70 to 79% answers were right 85% of the time. So a 79% ceiling understates real confidence. It passes the gate by never saying "confident play", not by predicting better.
- **What it costs:** no weather-only answer would ever say 80%+. A posted official status still decides as before.
- **Status:** built as `status_ceiling`, **off by default** (commit 3bab619). Turning it on is one setting, and it is Jimmy's call.

**Remaining confident wrong with the ceiling off (2):**
- **ut-baseball-2023-04-23**: **weather-unpredictable.** The game was moved a day early on a forecast that never showed up in the data (5% chance, no storms). Only the ceiling catches it.
- **cbb-bobcat-2019-02-19**: **effectively unpredictable from weather.** It was 43°F with 0.24 in that morning and a 5% chance in the window. The fresh cold-wet games show days like this almost always play.

**Selection optimism:** today I compared about 43 cap setups: 30 in the first Step 2 sweep, 7 fixed configurations in the Step 2 check, and 6 options here, re-run on the 154 set. So the best held-out numbers are slightly optimistic. The cold-wet cap and the ceiling were both set *before* the 25 new games were added, and those games test both.

**Commits on feat/more-fixtures (#47):** d4e0ee6 (25 cold-wet fixtures plus the selection script `staging/coldwet_select.py`) and 3bab619 (optional status ceiling, off). PARAMS are unchanged; the in-sample best point on 154 equals the current defaults (86% / 1). Full suite passes (474).

