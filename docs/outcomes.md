# "Was it on?" outcomes, drainage memory, feed and scoreboard

## Reporting an outcome
`POST /v1/outcomes` (or the MCP tool `report_outcome`) with `{field_id, date: YYYY-MM-DD, outcome: played|delayed|cancelled|unknown, game_time?, note?}`.

- **When:** after the event, for today or up to 7 days back. Future dates are rejected.
- **Same protections as player reports:**
  - Per-client hourly limits: 3 per venue and 10 in total.
  - Notes are limited to 280 characters, with control characters stripped.
  - Agents must only report what the user said, after the user agrees to share it.
- **No personal data:** no IPs and no names. The client address is used only, hashed, as a rate-limit key, and it is never stored in a record.
- **Storage:** Upstash when it is configured, otherwise in-memory. Records are kept for 400 days.
- **Test entries:** a report with `test: true`, a note marking it as a test, or the staging checklist's User-Agent is **marked as a test**. Test entries are kept in local memory only, never in the shared store, and are excluded from the memory, feed and scoreboard.

## Pre-event snapshots
Each live `/v1/status` answer stores the odds we gave for that game. The snapshot keeps the latest call before the start: at most one write every 30 minutes, nothing after the start, and nothing more than 7 days ahead. The scoreboard therefore compares outcomes with what we actually said in advance.

When an outcome is reported, the matching snapshot is attached to it.

## Per-venue drainage memory
A **wet day** is an event where we took points off for rain that had already fallen (`soak_penalty > 0`) and no official status was posted. For each venue, the memory works like this:

1. Count the wet days and how many of them went ahead (`k` of `n`). Compare with the number we expected, which is the sum of our odds.
2. Shrink toward our expectation with 4 pseudo-events:

   `rate = (k + 4·prior) / (n + 4)`

3. Convert the result into a multiplier on the soak penalty:

   `observed cancel rate / predicted cancel rate`, bounded to **0.4 to 1.6**.

The memory has **no effect until 3 wet days** have been reported.

`/v1/status` returns it in `model.drainage_memory`. When it is active, the reason says, for example: *"This field has played through 5 of 6 reported wet days (we expected about 3.0). Rain that already fell counts 0.6x here."*

## Feed and scoreboard
| What | Where |
|---|---|
| Outcomes feed (JSON) | `GET /v1/outcomes` |
| Outcomes feed (Atom) | `GET /v1/outcomes/feed.atom` |
| One venue's events and memory | `GET /v1/outcomes?field_id=…` |
| Scoreboard (JSON) | `GET /v1/outcomes/scoreboard` |
| Scoreboard (web page) | `/scoreboard` |

**Event outcome:** each event's outcome is the majority of its reports. "Delayed" counts as played. Ties and "unknown" are left out.

**Metrics:**
- **Directional:** odds of 50% or more and the event was played, or under 50% and it wasn't.
- **Confident wrong:** odds of 80% or more and the event wasn't played, or 20% or less and it was.
- **Brier score:** lower is better.

Metrics are reported overall, for model-only events (no official status) and by surface. Events reported without a snapshot are counted but not scored.
