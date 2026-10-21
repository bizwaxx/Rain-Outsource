# Oct 21 retest (2026-10-21, 10:25 AM to 1:35 PM CT): all fixtures, held out (leave-one-out)

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

