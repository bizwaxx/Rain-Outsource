# MCP server

Rainout Source runs a stateless MCP server (read-only except `report_cancellation`) using the **Streamable HTTP** transport:

```
https://rainout-agent-source.vercel.app/mcp
```

- JSON-RPC 2.0 over `POST` with plain JSON responses. There is no SSE stream and no session id. `GET` returns 405, which the spec allows.
- Protocol versions: `2025-06-18`, `2025-03-26` and `2024-11-05`. The server echoes the client's version when supported and otherwise answers with the newest one.
- No auth. Origin check against DNS rebinding: browser `Origin`s on localhost or private IPs are refused unless `RAINOUT_MCP_ALLOW_LOCAL=1`. `RAINOUT_MCP_ALLOWED_ORIGINS` (comma-separated) restricts origins to an allowlist.
- Limits: 256 KB request body and 20 messages per batch.

## Tools

| Tool | Use it for | Backed by |
|---|---|---|
| `find_team` | "I play on ATX Boys, Friday nights at Craig Fields" | `GET /v1/teams/search` |
| `get_games` | games and odds in a date range | `GET /v1/teams/{id}/games` |
| `check_game` | "do I have a game today?" (team_id + date) or a venue + time | teams API / `/v1/status` |
| `get_venue_status` | one venue at one time | `/v1/status` |
| `check_schedule` | score a whole schedule (ICS URL or list of games) | `/v1/schedule/check` |
| `report_cancellation` | the user says their game was rained out, delayed, the field is closed or they're playing | `POST /v1/reports` ([docs/reports.md](reports.md)) |

All tools are annotated `readOnlyHint: true` except `report_cancellation` (`readOnlyHint: false`, `destructiveHint: false`, `idempotentHint: false`), which stores an anonymous report in the cache for 24 hours, rate limited per client. Results come back as `structuredContent` and as JSON text. Problems such as an unknown team or a league site being down come back as `isError: true` with a plain-language message that tells the agent what to do next, for example "ask the user for their schedule link."

The tool descriptions and the `initialize` instructions tell the agent to:
1. confirm the team match with the user,
2. read the one-sentence `reason` with the odds,
3. **ask the user before adding anything to a calendar.** The server never writes to calendars. Each game carries a Google Calendar API event body (`calendar_event`, with a stable `iCalUID` so re-adding updates instead of duplicating) and `ics` text.

## Connecting

- **Claude** (Settings → Connectors → Add custom connector): URL `https://rainout-agent-source.vercel.app/mcp`.
- **ChatGPT** (developer mode / connectors): same URL, no auth. For GPT Actions instead, import `https://rainout-agent-source.vercel.app/openapi.yaml`.
- **Cursor / Claude Code / other clients:**

```json
{ "mcpServers": { "rainout": { "url": "https://rainout-agent-source.vercel.app/mcp" } } }
```

## Raw example

```bash
curl -s https://rainout-agent-source.vercel.app/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"check_game","arguments":{"team_id":"tsl-340-744621-4306365","date":"today"}}}'
```

See [agent-conversation.md](agent-conversation.md) for a full conversation and [`/llms.txt`](https://rainout-agent-source.vercel.app/llms.txt) for the short agent-facing summary.
