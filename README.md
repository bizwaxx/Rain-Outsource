# Rainout Source

**Mission Statement:**  
Rain-Outsource is building a free, open public API that delivers real-time weather and rainout status for any outdoor event across the United States.  
Our mission is simple: Make it effortless for anyone to know whether their outdoor event is still happening — whether it’s a little league game, a concert, a festival, or any outdoor gathering.  
We believe this information should be freely available to the public, voice assistants, apps, and developers with no restrictions.

Open agent-readable source for softball/baseball rainout status and game-time play probability.

**Creator:** JEEZ Labs

## Important status
The public docs and OpenAPI file are live on GitHub Pages. The first working backend is live on Vercel and also available in this repo as a Python app.

Live backend URL:

```text
https://rainout-agent-source.vercel.app
```

Live OpenAPI URL:

```text
https://rainout-agent-source.vercel.app/openapi.yaml
```

Current backend status:

1. Supports `/health` locally and on Vercel-style hosting.
2. Supports `/v1/fields` on live Vercel hosting so agents can discover supported fields and aliases.
3. Supports `/v1/status?field_id=Krieg&game_time=...` and `/v1/status?field_id=Havins&game_time=...` locally and on live Vercel hosting.
4. Supports `/api/status?field_id=Krieg&game_time=...` on Vercel-style hosting.
5. Resolves aliases like `Krieg`, `Havins`, `krieg field`, `havins complex`, and full field IDs.
6. Pulls live game-time weather from the National Weather Service API.
7. Checks the official Austin Athletics source page on each status request, but stays conservative: if there is no clear cancelled, delayed, or field-closed signal, official status remains `unknown` and the spoken answer tells the user to call the rainout line before leaving.

## Pilot fields

- Field: Krieg Field Softball Complex
- Address: 515 South Pleasant Valley Road, Austin, TX 78741
- Rainout phone: 512-978-2680
- Weather source: National Weather Service API

- Field: Havins Softball Complex
- Address: 12138 N Lamar Blvd, Austin, TX 78753
- Rainout phone: 512-978-2680
- Official source page: https://www.austintexas.gov/department/athletics
- Weather source: National Weather Service API

## Agent rule

Official rainout status wins. Weather gives drive risk and game-time play probability. If the official source is checked but unclear, keep status as `unknown` instead of guessing.

Use the word **rain**, not **rainfall**, in Dad-facing answers.

## Example answer

Official status is on. At game time, rain chance is 67% with storms likely. Estimated play probability is 25%. Check the rainout line again right before leaving.

## API contract

See `openapi.yaml`.

## Outside-agent test

Use this prompt in ChatGPT, Grok, Dad Agent, or another web-capable agent:

```text
You are checking an outdoor game status. Use the public Rainout Source API.
Open https://rainout-agent-source.vercel.app, read https://rainout-agent-source.vercel.app/openapi.yaml, discover supported fields at https://rainout-agent-source.vercel.app/v1/fields, then query Havins Field for 2026-06-06T20:20:00-05:00. Answer in plain English. If official status is unknown, do not guess; tell the user to call the rainout line before leaving. Use the word rain, not rainfall.
```

More examples: `docs/agent-examples.md`.

## Local test

```bash
python -m pytest -q
python examples/krieg_820_demo.py
```

## Run the backend locally

```bash
python -m rainout_agent.http_app
```

Then open:

```text
http://127.0.0.1:8000/v1/status?field_id=Krieg&game_time=2026-06-05T20:20:00-05:00
```

## Public hosting plan

Recommended first public host:

- GitHub public repo: `rainout-source`
- GitHub Pages for docs and `openapi.yaml`
- Later: Cloudflare Worker or Vercel endpoint for live `/v1/status`
