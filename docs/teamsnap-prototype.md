# TeamSnap prototype (draft, not live)

Reads a TeamSnap team's games and adds Rain-Outsource play odds.
Official status comes first. Odds are always labelled as an estimate.

- Code: `rainout_agent/teamsnap.py`, CLI: `scripts/teamsnap_odds.py`, tests: `tests/test_teamsnap.py`
- Data: mock TeamSnap API v3 responses in `tests/fixtures/teamsnap/` (test data, not a real team).
- Not wired into any Vercel function. No new function, no route, nothing deployed.

## Run it
    python scripts/teamsnap_odds.py                          # fixtures + demo weather, no network
    python scripts/teamsnap_odds.py --live-weather --rebase  # fixtures + real NWS weather
    TEAMSNAP_TOKEN=... python scripts/teamsnap_odds.py --team-id <id> --live-weather

The real API is called only when `TEAMSNAP_TOKEN` is set. The token is only sent to
api.teamsnap.com as a Bearer header and is never printed or logged.

## Order of truth per game
1. Cancelled in TeamSnap (`is_canceled`) -> "cancelled".
2. Venue official source (rainout page/line), when posted.
3. Otherwise our estimate (same model as /v1/status), labelled "estimate".

## What real access needs
- A TeamSnap account, plus an OAuth2 app registered at https://auth.teamsnap.com (client id + secret, redirect URL).
- Each team admin signs in and approves read access (scope `read`). That gives an access token.
- No sandbox is documented; testing needs a real (free) TeamSnap team.

## Docs used
- https://github.com/teamsnap/teamsnap-javascript-sdk (APIv3 SDK + OAuth quick start)
- https://github.com/teamsnap/teamsnap_rb (APIv3 Ruby client)
- https://github.com/teamsnap/omniauth-teamsnap (OAuth2 app id/secret, scopes)
- http://amundsen.com/media-types/collection/ (Collection+JSON)
- https://slides.com/dmccraw/teamsnap_and_graphql (TeamSnap APIv3 event template and events/search filters)
