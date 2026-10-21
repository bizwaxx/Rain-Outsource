# NWS severe and tropical alerts as a model input (Oct 21, 2026, 4:05 PM CT): tried, NOT earned, left OFF

Branch `feat/nws-alerts-model` (off `tune/soccer-pro-rain` <- `backtest/soccer-2026-10-21` <- `feat/more-fixtures`). Draft PR, never merge. Not for launch: #47, /accuracy and docs/accuracy.md unchanged.

## What was built
- `rainout_agent/nws_alerts.py`: live active alerts by point from free api.weather.gov (`/alerts/active?point=`), NWS User-Agent, 3-minute shared cache, only for events from 3 h ago to 12 h ahead. Kill switch `RAINOUT_NWS_ALERTS`, **default off**. Alert items use the same shape as #49's `nws_lightning_alerts` (event, level, headline, severity, onset, ends, sender, url) plus `kind`.
- Covered: Severe Thunderstorm, Tornado, Flash Flood, Tropical Storm and Hurricane warnings; the same watches plus Flood Watch (NWS merged Flash Flood Watch into Flood Watch).
- Features as known at the start: `alert_warn` (a warning in effect at the start or the hour before), `alert_watch` (a watch or warning then), `alert_tropical` (a tropical storm or hurricane watch/warning in the 24 h before).
- `event_risk`: three optional caps, `alert_warn_cap`, `alert_watch_cap`, `alert_tropical_cap` (all None = off). Wired into `event_weather.evaluate` (no new function: 11 on this branch, and the launch set stays at 12).

## History for the backtest
Free Iowa Environmental Mesonet VTEC archive, `json/vtec_events_bypoint.py` (storm-based warning polygons plus the county/zone of the point), 2 days before to 1 day after each event; 175 requests, cached, all succeeded. All 175 events are in the US, so none is "no data". Only alerts issued at or before the start count.
Coverage: 25 of 175 events had a watch or warning at the start (15 not played, 10 played); 12 had a warning (9 / 3); 5 had tropical alerts (4 / 1).

## Backtest (nested leave-one-out, ties to "off")
Rule grid: warning cap {off, 45, 21} x watch cap {off, 69, 50} x tropical cap {off, 45, 21} x college/pro soccer rain tolerance {off, 0.5/0.5, 0/0} = **81 settings**, x 82,944 base grid points = **6,718,464 combinations**. One run.

| Held out | Before | After |
|---|---|---|
| All 175 | 82% / 2 CW | 82% / 2 CW |
| Soccer (25) | 76% / 0 | 76% / 0 |
| Baseball (82) | 85% / 2 | 85% / 2 |
| Softball (35) | 86% / 0 | 86% / 0 |
| Motocross (33) | 76% / 0 | 76% / 0 |
Every fold picked "all off". In-sample, the best is a 3-way tie at loss 30 (off; watch cap 69; watch cap 50); the best with the soccer rule on is 31.

**The 2 confident-wrong calls are not alert-driven.** Neither ut-baseball-2023-04-23 nor cbb-bobcat-2019-02-19 had any watch or warning (none at the start, none during the game). Alerts cannot clear them.

**Soccer pro-rain misses vs the 7 cancellations: partly.** With the rain tolerance on and a tropical cap of 21 (default params), the 4 games played in heavy rain become right (50-79%), and the 4 hurricane/tropical-storm cancellations (Hilary x2, Houston 2024, Red Bulls 2021) stay right at 21%. But Philadelphia (Flood Watch only), Louisville (Tornado Watch only) and NC Courage (Florence; no alert covering the venue at the start) go wrong, and San Diego Wave (played through a Tropical Storm Warning) drops to 21%. Net: soccer 19/25 -> 18/25 on the default parameters. Watches don't separate played from not played (10 played vs 15 not); tropical warnings do (4 of 5), but 5 events is too few to earn a rule.

## Plain answer
Not earned. Keep `RAINOUT_NWS_ALERTS` off and all caps off. Showing active warnings to users as information (like #49 does for lightning) is still reasonable; using them to move the odds is not supported by these 175 events.
