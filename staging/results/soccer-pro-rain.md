# College/pro soccer rain tolerance (Oct 21, 2026, 3:45 PM CT): tried, NOT earned, left OFF

Branch `tune/soccer-pro-rain` (off `backtest/soccer-2026-10-21`). Not for launch: #47, /accuracy and docs/accuracy.md unchanged.

**Rule (in rainout_agent/event_risk.py, off by default):** for soccer at a college/pro venue (`venue_profile.level == "college_pro"`, 23 of 25 soccer fixtures), the window rain chance counts at `pro_rain_factor` and the soak at `pro_soak_factor`. Storm, wind, cold, threat, unstable-air caps and the cancel floor are unchanged.

**Honest hold-out:** nested leave-one-out. For each of the 175 fixtures, BOTH the rule setting and every other model axis were chosen on the other 174 only (caps fixed as on #47: threat 79, unstable-air 2500, cancel floor 21). Ties go to "rule off", so the data has to earn it.
**Settings tried:** 20 rule settings (pro_rain_factor {off, 0.7, 0.5, 0.3, 0} x pro_soak_factor {off, 0.5, 0.2, 0}) x 82,944 base grid points = 1,658,880 combinations. One run, no other variants.

| Held out | Before | After |
|---|---|---|
| All 175 | 82% / 2 CW | 82% / 2 CW |
| Soccer (25) | 76% / 0 | 76% / 0 |
| College/pro soccer (23) | 74% / 0 | 74% / 0 |
| Baseball (82) | 85% / 2 | 85% / 2 |
| Softball (35) | 86% / 0 | 86% / 0 |
| Motocross (33) | 76% / 0 | 76% / 0 |
Every one of the 175 folds (and the in-sample fit) picked "rule off". No baseball, softball or motocross prediction changed.

**Why it doesn't help:** in these weather features, the 4 pro games played in heavy rain (the misses at 21%) look the same as the 7 pro games that were not played. The other 2 soccer misses are a 28°F start and 48 mph gusts, which this rule doesn't touch. Both groups have a 77-100% rain chance in the game window, most had inches of rain already, and none has a thunder flag. With the rule fully on (factors 0), the 4 played misses go from 21% to 79% (right), but 7 not-played games also go to 79% (wrong). Those were called off for hurricanes (Hilary x2, Florence), severe-weather threats, a waterlogged pitch in torrential storms, or a field judged unsafe after a long weather delay. The hourly archive features don't separate those from ordinary heavy rain.
**Next step that could work:** severe-weather and tropical-storm alerts (NWS warnings), or a real lightning signal (the parked lightning work, #49), not a blanket rain tolerance.

Files: soccer-pro-rain/tune_pro.py, soccer-pro-rain/results.json (per-fixture before/after and fold choices). Tests: tests/test_rain_tolerant.py.
