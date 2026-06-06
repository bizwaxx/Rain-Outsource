# Rainout Source

**Mission Statement:**  
Rain-Outsource is building a free, open public API that delivers real-time weather and rainout status for any outdoor event across the United States.  
Our mission is simple: Make it effortless for anyone to know whether their outdoor event is still happening — whether it’s a little league game, a concert, a festival, or any outdoor gathering.  
We believe this information should be freely available to the public, voice assistants, apps, and developers with no restrictions.

Open agent-readable source for softball/baseball rainout status and game-time play probability.

**Creator:** JEEZ Labs

## Important status
This project is ready to publish, but it is **not online until GitHub or another public host is connected**.

For ChatGPT, Claude, Grok, Dad Agent, and other agents to use it broadly, we still need to:

1. Publish this repository publicly.
2. Host the API endpoint.
3. Publish the OpenAPI file at a stable URL.
4. Add live source checking for official rainout numbers/pages.
5. Register or document the API so agents can discover it.

## Pilot field

- Field: Krieg Field Softball Complex
- Address: 515 South Pleasant Valley Road, Austin, TX 78741
- Rainout phone: 512-978-2680
- Weather source: National Weather Service API

## Agent rule

Official rainout status wins. Weather gives drive risk and game-time play probability.

Use the word **rain**, not **rainfall**, in Dad-facing answers.

## Example answer

Official status is on. At game time, rain chance is 67% with storms likely. Estimated play probability is 25%. Check the rainout line again right before leaving.

## API contract

See `openapi.yaml`.

## Local test

```bash
python -m pytest -q
python examples/krieg_820_demo.py
```

## Public hosting plan

Recommended first public host:

- GitHub public repo: `rainout-source`
- GitHub Pages for docs and `openapi.yaml`
- Later: Cloudflare Worker or Vercel endpoint for live `/v1/status`
