# Rainout Source Agent Examples

Use these examples to test whether outside agents can discover and query Rainout Source.

## Live discovery URLs

- Service discovery: https://rainout-agent-source.vercel.app
- OpenAPI contract: https://rainout-agent-source.vercel.app/openapi.yaml
- Supported fields: https://rainout-agent-source.vercel.app/v1/fields
- Krieg status example: https://rainout-agent-source.vercel.app/v1/status?field_id=Krieg&game_time=2026-06-06T20:20:00-05:00

## ChatGPT / Grok / general agent prompt

```text
You are checking an outdoor game status. Use the public Rainout Source API.

1. Open this service discovery URL: https://rainout-agent-source.vercel.app
2. Read the OpenAPI contract at: https://rainout-agent-source.vercel.app/openapi.yaml
3. Discover supported fields at: https://rainout-agent-source.vercel.app/v1/fields
4. Query Krieg Field for this game time: 2026-06-06T20:20:00-05:00
5. Answer in plain English and cite the official rainout phone/source and weather source.
6. If official rainout status is unknown, do not guess. Say that the user should call the rainout line before leaving.
7. Use the word "rain," not "rainfall."
```

## Expected safe answer pattern

```text
Official rainout status is unknown for Krieg Field Softball Complex. At game time, rain chance is [number]% [with/without storms possible]. Estimated play probability is [number]%. Call the rainout line, 512-978-2680, before leaving. Weather source: National Weather Service API.
```

## curl smoke tests

```bash
curl -s https://rainout-agent-source.vercel.app | python -m json.tool
curl -s https://rainout-agent-source.vercel.app/v1/fields | python -m json.tool
curl -s 'https://rainout-agent-source.vercel.app/v1/status?field_id=Krieg&game_time=2026-06-06T20:20:00-05:00' | python -m json.tool
```

## Agent integration rule

Official rainout status wins. Weather gives game-time risk and play probability. When the official status is unknown, agents must say so and point users to the official rainout phone before they drive.
