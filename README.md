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
2. Supports `/v1/fields` on live Vercel hosting so agents can discover supported fields, ownership type, aliases, status URLs, and official sources.
3. Supports `/v1/status?field_id=Krieg&game_time=...`, `/v1/status?field_id=Craig&game_time=...`, `/v1/status?field_id=Havins&game_time=...`, plus the Austin metro public/private expansion fields locally and on live Vercel hosting.
4. Supports `/api/status?field_id=Krieg&game_time=...` on Vercel-style hosting.
5. Resolves aliases like `Krieg`, `Craig`, `Havins`, `Northeast Metro`, `Southeast Metro`, `Oak Hill Youth Sports`, `Manchaca Optimist`, `T&C Sports`, `Balcones`, `NWLL`, `Western Hills`, `Old Settlers Baseball`, `OSP Softball`, `Brushy Creek Softball`, `Milburn Park`, `Downs Field`, `Anderson High School`, `Georgetown Fields`, `Field of Dreams`, `DSYSA Baseball`, and full field IDs.
6. Pulls live game-time weather from the National Weather Service API.
7. Checks each field's official source page on each status request, but stays conservative: if there is no clear cancelled, delayed, or field-closed signal, official status remains `unknown` and the spoken answer tells the user to call the rainout line or check the official source before leaving.
8. Returns `answer_requirements` so outside agents must include field name, official status, rain chance, play probability, and rainout phone or official source in their answer.

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

- Field: Northeast Metropolitan Park
- Ownership: public
- Address: 2703 Pecan Street, Pflugerville, TX 78660
- Official source page: https://parks.traviscountytx.gov/parks/northeast-metro

- Field: Southeast Metropolitan Park
- Ownership: public
- Address: 4511 Highway 71 East, Del Valle, TX 78617
- Official source page: https://parks.traviscountytx.gov/parks/southeast-metro

- Field: Oak Hill Youth Sports Association
- Ownership: private nonprofit
- Address: 6300 Joe Tanner Lane, Austin, TX 78749
- Official source page: https://www.ohysa.com/

- Field: Manchaca Optimist Youth Sports Complex
- Ownership: private nonprofit
- Address: West FM 1626, Manchaca, TX 78652
- Official source page: https://www.facebook.com/ManchacaOptimist/

- Field: Town & Country Sports Complex
- Ownership: private nonprofit
- Address: 9100 Meadowheath Drive, Austin, TX 78729
- Official field status page: https://www.tandcsports.org/field-status/

- Field: Balcones Youth Sports
- Ownership: private nonprofit
- Address: 3106 Adelphi Lane, Austin, TX 78727
- Official field status page: https://www.balconesyouthsports.org/

- Field: Northwest Little League
- Ownership: private nonprofit
- Address: 3105 Hunt Trail, Austin, TX 78757
- Official field status page: https://www.nwll-austin.org/

- Field: Western Hills Little League
- Ownership: private nonprofit
- Address: Austin, TX
- Official source page: https://www.westernhillslittleleague.com/

- Field: Old Settlers Baseball Complex
- Ownership: public
- Address: 3300 E Palm Valley Blvd, Round Rock, TX 78665
- Official source page: https://www.roundrocktexas.gov/city-departments/parks-and-recreation/old-settlers-baseball/
- Parks phone: 512-218-5540

- Field: Old Settlers Hall of Fame Softball Complex
- Ownership: public
- Address: 3300 E Palm Valley Blvd, Round Rock, TX 78665
- Official source page: https://www.roundrocktexas.gov/city-departments/parks-and-recreation/old-settlers-softball/
- Parks phone: 512-218-5540

- Field: Brushy Creek Sports Park Softball Fields
- Ownership: public
- Address: 2310 Brushy Creek Road, Cedar Park, TX 78613
- Official field status page: https://www.cedarparktexas.gov/947/Athletic-Facilities

- Field: Elizabeth Milburn Park Multipurpose Fields
- Ownership: public
- Address: 1901 Sun Chase Blvd, Cedar Park, TX 78613
- Official field status page: https://www.cedarparktexas.gov/947/Athletic-Facilities

- Field: Downs Field
- Ownership: public / league-used
- Address: 2816 E. 12th Street, Austin, TX 78702
- Official league field page: https://www.austinmetrobaseball.com/fields.php

- Field: Anderson High School Baseball Field
- Ownership: school / league-used
- Address: 8403 Mesa Drive, Austin, TX 78759
- Official league field page: https://www.austinmetrobaseball.com/fields.php

- Field: San Gabriel Park - GYBA Field 8
- Ownership: public / league-used
- Address: E. Morrow Street & N. College Street, Georgetown, TX 78626
- Official league field page: https://www.austinmetrobaseball.com/fields.php

- Field: CTX Field of Dreams
- Ownership: private / league-used
- Address: 7398 Creek Rd, Dripping Springs, TX 78620
- Official directory page: https://www.atxyouthbaseball.com/directory-resources/ctx-field-of-dreams

- Field: DSYSA Baseball & Softball Sports Complex
- Ownership: private nonprofit
- Address: 520 Sportsplex Drive, Dripping Springs, TX 78620
- Official DSYSA status/protocol page: https://www.dsysasports.org/fields

- Field: DSYSA Sports Park Fields
- Ownership: private nonprofit
- Address: 370 Sports Park Rd, Dripping Springs, TX 78620
- Official DSYSA status/protocol page: https://www.dsysasports.org/fields

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
Open https://rainout-agent-source.vercel.app, read https://rainout-agent-source.vercel.app/openapi.yaml, discover supported fields at https://rainout-agent-source.vercel.app/v1/fields, then query Havins Field for 2026-06-06T20:20:00-05:00. Follow the `answer_requirements` returned by the API. Answer in plain English. If official status is unknown, do not guess; clearly say official status is unknown and tell the user to call 512-978-2680 before leaving. Use the word rain, not rainfall.
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
