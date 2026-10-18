# Nationwide venues from OpenStreetMap

Rainout Source has two tiers of venues:

| verification_level | What it means | Where it lives |
| --- | --- | --- |
| `confirmed` | Hand-verified: name, address, coordinates, operator, official rainout source or phone where one exists, documented history. | `data/<city>/*.json`, `data/motocross/tracks.json` |
| `osm_only` | Built automatically from OpenStreetMap. Name and location are community map data. No official status source, phone, surface or drainage is known, so answers are weather and NOAA history only. | `data/osm/<ST>.json` |

Every API response that includes an OSM venue labels it `verification_level: "osm_only"`, adds a
`verification_note`, links the OSM elements, and carries the attribution
"Map data (c) OpenStreetMap contributors, ODbL 1.0".

## Attribution and license

Venue names, locations and addresses in `data/osm/` are derived from
[OpenStreetMap](https://www.openstreetmap.org/copyright) data, (c) OpenStreetMap contributors, and are
available under the [Open Database License (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/1-0/).
`data/osm/` is a derivative database under the ODbL: if you redistribute it, keep the attribution
and share it under the same license. The extract comes from [Geofabrik](https://download.geofabrik.de/north-america/us.html).
Rain history in `data/noaa/stations.json` is NOAA NCEI GHCN-Daily data (US government, public domain).

## Using it

```text
GET /v1/fields?q=Lamar%20Park&state=TX          name search (confirmed + osm_only, best match first)
GET /v1/fields?near=30.27,-97.74&radius_km=10     nearby venues, sorted by match then distance
GET /v1/fields?state=FL&sport=soccer&verification=osm_only&limit=100&offset=0
GET /v1/status?field_id=osm-tx-...&game_time=...  works with any OSM venue id
GET /v1/tracks?state=TX                          confirmed + OSM motocross tracks
GET /v1/tracks/osm-tx-.../check?date=saturday    no event calendar: answer + conditions_if_racing
GET /v1/tracks/osm-tx-.../history                NOAA monthly stats from the shared station
```

Without `q` or `near`, `/v1/fields` keeps its original behaviour (confirmed venues only) and
`/v1/tracks` lists the 10 confirmed tracks. OSM venues need `state`, `q` or `near`, and results are
paginated (`limit` up to 500, `offset`, `next_offset`). MCP: `find_venue` searches both tiers;
`get_venue_status` accepts an OSM `field_id`; `find_track` returns OSM motocross tracks labeled
`osm_only`; `check_race` on an OSM track (or on any date with no listed event) adds
`conditions_if_racing`, a weather-only estimate that assumes racing 8am-5pm local.

## Coverage

The rollout is state by state. `data/osm/index.json` (and `osm_coverage` in `/v1/fields` responses) lists
`full_states` (ball fields, soccer complexes and motocross) and `motocross_only_states`.
OSM data as of the Geofabrik extract of 2026-10-16.

<!-- counts -->
| State | Venues | Ball fields | Soccer | Multipurpose | Motocross | With NOAA station |
| --- | --- | --- | --- | --- | --- | --- |
| AK | 148 | 90 | 36 | 21 | 1 | 141 |
| AL | 976 | 759 | 81 | 133 | 3 | 973 |
| AR | 503 | 382 | 73 | 47 | 1 | 503 |
| AZ | 1,243 | 675 | 212 | 350 | 6 | 1,217 |
| CA | 5,599 | 3,512 | 911 | 1,152 | 24 | 5,584 |
| CO | 1,342 | 719 | 285 | 334 | 4 | 1,341 |
| CT | 925 | 513 | 142 | 270 | 0 | 925 |
| DC | 127 | 56 | 49 | 22 | 0 | 127 |
| DE | 157 | 86 | 18 | 53 | 0 | 157 |
| FL | 2,246 | 1,373 | 375 | 498 | 0 | 2,243 |
| GA | 1,340 | 836 | 219 | 282 | 3 | 1,322 |
| HI | 205 | 164 | 19 | 21 | 1 | 204 |
| IA | 1,355 | 1,047 | 154 | 154 | 0 | 1,353 |
| ID | 369 | 266 | 41 | 58 | 4 | 369 |
| IL | 3,294 | 2,162 | 449 | 679 | 4 | 3,294 |
| IN | 1,286 | 859 | 180 | 244 | 3 | 1,286 |
| KS | 800 | 561 | 101 | 136 | 2 | 799 |
| KY | 619 | 417 | 78 | 122 | 2 | 617 |
| LA | 580 | 466 | 47 | 67 | 0 | 571 |
| MA | 1,955 | 1,126 | 332 | 495 | 2 | 1,955 |
| MD | 1,446 | 906 | 210 | 328 | 2 | 1,446 |
| ME | 575 | 368 | 63 | 143 | 1 | 566 |
| MI | 2,685 | 1,655 | 397 | 621 | 12 | 2,681 |
| MN | 2,026 | 1,390 | 185 | 445 | 6 | 2,019 |
| MO | 1,138 | 650 | 244 | 238 | 6 | 1,138 |
| MS | 402 | 307 | 27 | 68 | 0 | 402 |
| MT | 294 | 203 | 57 | 31 | 3 | 292 |
| NC | 1,935 | 1,318 | 256 | 358 | 3 | 1,935 |
| ND | 193 | 132 | 33 | 27 | 1 | 185 |
| NE | 670 | 411 | 139 | 117 | 3 | 670 |
| NH | 529 | 277 | 76 | 173 | 3 | 529 |
| NJ | 1,663 | 926 | 235 | 501 | 1 | 1,663 |
| NM | 366 | 228 | 72 | 61 | 5 | 359 |
| NV | 341 | 240 | 39 | 60 | 2 | 336 |
| OK | 601 | 434 | 86 | 80 | 1 | 593 |
| TX | 3,933 | 2,404 | 804 | 720 | 5 | 3,903 |

Full states: 36. Venues: 43,914 (incl. 162 motocross tracks). By kind: ball_field 27,918, soccer_complex 6,725, multipurpose_field 9,109, motocross_track 114. By sport tag: baseball 35,877, motocross 114, soccer 15,834, softball 4,245.
<!-- /counts -->

Motocross-only states still list their OSM motocross tracks.

## Storage design

Tens of thousands of venues don't fit the per-venue JSON plus per-venue history model, and there's no
database: everything is static files bundled with the Vercel functions.

* **Per-state compact index** `data/osm/<ST>.json`: `{"row_fields": [...], "rows": [[...]]}`, one row per
  venue (id, name, sports, kind code, lat, lon, city, address, pitch count, OSM element refs, NOAA
  station id). About 190 bytes a venue. The venue id embeds the state (`osm-tx-<name-slug>-w123`),
  so a lookup opens only one state file; files are parsed once per warm function instance (LRU cache).
* `data/osm/motocross.json`: all OSM motocross tracks nationwide (track search without a state).
* `data/osm/index.json`: counts by state, kind and sport, the OSM data date and the license.
* **Shared rain history per NOAA station** `data/noaa/stations.json`: monthly stats only
  (days observed, mean rain, wet-day share and the share of days reaching 0.10/0.25/0.50/0.75/1.00 in)
  for 2016-2025, about 750 bytes a station. Venues store just the station id. At request time the
  sport's threshold (ball 0.5 in, soccer 0.75 in, motocross 1.0 in) turns the station's monthly stats into the
  same `noaa_history` calibration block the confirmed venues use, so play odds are computed the same way.
* Hand-verified venues and their own NOAA files are unchanged and always win over OSM duplicates.

No Supabase or other paid service is needed: the whole dataset is a few MB of JSON.

## Pipeline (reproducible)

`scripts/osm_venues.py` (needs `osmium-tool`, e.g. `apt-get install osmium-tool`):

1. **download**: the Geofabrik US extract (`us-latest.osm.pbf`) and the per-state `.poly` boundaries.
   (Public Overpass servers refused or timed out on nationwide queries, so the pipeline uses Geofabrik.)
2. **filter**: `osmium tags-filter` keeps sport-tagged features, named parks / sports centres /
   recreation grounds / stadiums / tracks, schools, raceways and place nodes.
3. **extract**: `osmium extract -p <state>.poly`, then `osmium export` to GeoJSON lines.
4. **build**:
   * Pitches are `leisure=pitch` with `sport` in baseball, softball, soccer (named `sports_centre` /
     `stadium` with those sports count too). Motocross is `sport=motocross|supercross` on any element
     (`leisure=track`, `highway=raceway`, `leisure=sports_centre`, ...).
   * A pitch inside a named park / sports centre / school polygon (smallest one, under 3 km across)
     belongs to that venue. Other pitches are clustered by 150 m single linkage and named from a real
     pitch name or the nearest named park or school within 250 m. Motocross pieces cluster at 800 m.
   * Unnamed clusters are dropped (we can't present a nameless venue to a user); names like
     "batting cages" or "bullpen" are skipped.
   * Kind: `ball_field`, `soccer_complex`, `multipurpose_field` (both) or `motocross_track`.
   * Address: OSM `addr:*` tags when present, else "City, ST" / "near Town, ST" from the nearest
     city or town (no paid geocoder).
   * Dedupe: an OSM venue within 500 m (1.5 km for motocross) of a confirmed venue with a shared sport,
     or within 3 km with a similar name, is dropped and recorded in `data/osm/confirmed_matches.json`.
   * Features that straddle a state line are kept only in the state containing them.
5. **stations**: for each venue the nearest GHCN-Daily station (within 40 km) with PRCP from 2016
   through 2025; daily PRCP is fetched from NOAA's Access Data Service 25 stations per request and
   reduced to monthly stats; stations under 75% coverage are rejected and the next one is tried.

```bash
scripts/osm_venues.py build --pbf /path/us-latest.osm.pbf            # all states
scripts/osm_venues.py stations --states TX,OK,LA,FL                  # NOAA for these states + all motocross
```

## Caveats

* OSM coverage is uneven: many real complexes are mapped without names (dropped) and some not at all.
  Motocross is especially sparse.
* Sport tags are taken literally: a field tagged only `baseball` is not assumed to host softball.
* Time zones for OSM venues are approximate in split states (state plus a longitude rule).
* `osm_only` venues have no official status source; the odds are weather plus NOAA history only.
* Venues more than 40 km from a usable NOAA station get no history calibration.
