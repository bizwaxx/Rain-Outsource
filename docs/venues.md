# Venue schema and verified venues

Every venue Rainout Source supports is one JSON file under `data/`. The legacy
Texas ball fields live in metro folders (`data/austin/`, `data/dallas/`, ...);
new venues live in `data/venues/<state>/`. Historical NOAA files in
`data/historical/` are not venues.

The formal schema is [`schema/venue.schema.json`](../schema/venue.schema.json),
mirrored by a dependency-free validator in `rainout_agent/venue_schema.py`.
`tests/test_venue_schema.py` validates every venue on each test run.

## Fields

| Key | Required | Notes |
| --- | --- | --- |
| `id` | yes | Lowercase slug, usually `<city>-<state>-<venue>` |
| `field_name` | yes | Display name |
| `venue_type` | yes | `ball_field`, `soccer_complex`, `motocross_track`, `multipurpose_field` |
| `sports` | yes | One or more of `baseball`, `softball`, `soccer`, `motocross`, `multipurpose`; must fit `venue_type` |
| `sport` | yes | Legacy free-text label kept for API compatibility (also picks the NOAA rain-to-cancel threshold) |
| `city`, `state`, `address` | yes | `state` is a 2-letter USPS code |
| `coordinates` | yes | `{lat, lon}` inside the US |
| `coordinates_source` | yes | `method` (`official_venue_source`, `openstreetmap_cross_checked`, `place_listing_cross_checked`, `legacy`), plus `sources` (public URLs) and `verified_on` for every non-legacy method |
| `operator` | yes | Operator, owner or league; `null` when not verified |
| `surface` | yes | `grass`, `artificial_turf`, `dirt`, `grass_with_dirt_infield`, `mixed`, `unknown` |
| `drainage` | yes | `poor`, `average`, `good`, `unknown`; scales the rain-to-cancel threshold (0.75x / 1x / 1.5x) |
| `official_status_source_url` | yes | Official status or info page, or `null` |
| `rainout_phone` | yes | Published rainout line or `not_published` |
| `weather_source`, `aliases` | yes | |
| `official_status_source_name`, `status_source_type`, `poll_official_status`, `ownership_type`, `rain_to_cancel_threshold_in`, `agent_rule` | no | |
| `multi_site` | no | `true` for a league that plays at many parks (its coordinates are its main field); OSM parks near it are not treated as duplicates |

## Verification standard

No venue or coordinate is invented. A new venue is added only when:

1. its name and address are confirmed on a public source (the operator's or
   venue's own site, a city/county parks page, or a league page), and
2. its coordinates come from the venue itself (`official_venue_source`), or
   from an OpenStreetMap feature with the same name whose position agrees with
   the confirmed address (US Census geocoder and/or Nominatim within 1 km, or a
   reverse geocode onto the same road when the address does not geocode)
   (`openstreetmap_cross_checked`), or from a public place listing that the
   address geocode confirms (`place_listing_cross_checked`).

Candidates that failed these checks were left out (see the PR description).
`surface` and `drainage` stay `unknown` unless a source states them; motocross
tracks are `dirt`. New venues have `poll_official_status: false` until a
reliable machine-readable status signal is confirmed, so their official status
stays `unknown` and agents are told to check the official source.

## Adding a venue

1. Add `data/venues/<state>/<id>.json` following the schema.
2. Run `python scripts/fetch_noaa_history.py --field <id>` to build its NOAA history.
3. Run `python -m pytest -q`.

## Coverage

| State | ball_field | multipurpose_field | soccer_complex | motocross_track | Total |
|---|---|---|---|---|---|
| FL | 0 | 0 | 5 | 3 | 8 |
| LA | 0 | 0 | 2 | 0 | 2 |
| OK | 0 | 0 | 4 | 2 | 6 |
| TX | 34 | 1 | 12 | 5 | 52 |
| **All** | 34 | 1 | 23 | 10 | **68** |

## Verified venues (added with the schema)

| Venue | Type | City | Lat, Lon | Method | Sources |
| --- | --- | --- | --- | --- | --- |
| Dade City Motocross | motocross track | Dade City, FL | 28.34769, -82.20557 | openstreetmap_cross_checked | [1](https://www.openstreetmap.org/node/12732322614) [2](https://dadecitymx.com/new/contact/) |
| Mesa Motocross Park | motocross track | Fellsmere, FL | 27.7704, -80.57787 | openstreetmap_cross_checked | [1](https://www.openstreetmap.org/way/703107727) [2](https://mesamotocross.com/) |
| Orlando MX Park | motocross track | Orlando, FL | 28.53845, -81.09009 | openstreetmap_cross_checked | [1](https://www.openstreetmap.org/way/1480883180) [2](https://orlandomxpark.com/) [3](https://mxsports.com/event/orlando-mx-park) |
| Bob Hayes Sports Complex | soccer complex | Jacksonville, FL | 30.39683, -81.72523 | openstreetmap_cross_checked | [1](https://www.openstreetmap.org/relation/13461578) [2](https://www.jacksonville.gov/departments/parks-and-recreation/jaxparks/all-parks/bob-hayes-sports-complex-and-legends-center) |
| Kendall Soccer Park | soccer complex | Miami, FL | 25.69234, -80.3991 | openstreetmap_cross_checked | [1](https://www.openstreetmap.org/way/234378955) [2](https://www.miamidade.gov/global/recreation/park/kendall-soccer-park.page) |
| Econ Soccer Complex | soccer complex | Orlando, FL | 28.57298, -81.27466 | openstreetmap_cross_checked | [1](https://www.openstreetmap.org/relation/1161881) [2](https://www.orangecountyfl.net/cultureparks/parks.aspx?d=52&m=dtlvw) |
| West Pines Soccer Park & Preserve | soccer complex | Pembroke Pines, FL | 26.00461, -80.41055 | openstreetmap_cross_checked | [1](https://www.openstreetmap.org/way/985851040) [2](https://www.ppines.com/Facilities/Facility/Details/West-Pines-Soccer-Park-amp-Preserve-29) |
| Shimberg Sports Complex | soccer complex | Tampa, FL | 28.01319, -82.55679 | openstreetmap_cross_checked | [1](https://www.openstreetmap.org/way/145640589) [2](https://hcfl.gov/locations/shimberg-sports-complex) |
| BREC Burbank Soccer Complex | soccer complex | Baton Rouge, LA | 30.34599, -91.0985 | openstreetmap_cross_checked | [1](https://www.openstreetmap.org/way/468539918) [2](https://www.brec.org/facility/BurbankSoccerComplex) [3](https://brsoccer.org/fields/burbank-soccer-complex) |
| LaGrange Soccer Park | soccer complex | Lake Charles, LA | 30.19173, -93.2209 | openstreetmap_cross_checked | [1](https://www.openstreetmap.org/way/867768031) [2](https://www.cityoflakecharles.com/egov/apps/locations/facilities.egov?id=13&view=detail) |
| Sweet 16 MX | motocross track | Muskogee, OK | 35.78987, -95.33304 | openstreetmap_cross_checked | [1](https://www.openstreetmap.org/way/1434929836) [2](https://mxparks.com/motocross-tracks/oklahoma/sweet-16-mx-muskogee) |
| Oklahoma Motorsports Complex | motocross track | Norman, OK | 35.18619, -97.48829 | openstreetmap_cross_checked | [1](https://www.openstreetmap.org/way/1259498051) [2](https://www.oklahomamotorsportscomplex.com/track.html) |
| Service Blake Soccer Complex | soccer complex | Edmond, OK | 35.66381, -97.50191 | openstreetmap_cross_checked | [1](https://www.openstreetmap.org/way/488722121) [2](https://www.edmondok.gov/facilities/facility/details/Service-Blake-Soccer-Complex-22) [3](https://www.edmondsoccer.com/maps) |
| Alsuma Soccer Complex | soccer complex | Tulsa, OK | 36.09194, -95.86618 | openstreetmap_cross_checked | [1](https://www.openstreetmap.org/way/522228579) [2](https://travelsports.com/facilities/alsuma-soccer-complex) |
| Bill Boevers Soccer Complex | soccer complex | Tulsa, OK | 36.11026, -95.82228 | openstreetmap_cross_checked | [1](https://www.openstreetmap.org/way/250009032) [2](https://unionsoccerclub.org/field-map) |
| Mohawk Sports Complex | soccer complex | Tulsa, OK | 36.23172, -95.93558 | openstreetmap_cross_checked | [1](https://www.openstreetmap.org/way/281607526) [2](https://mohawksportscomplex.com/location-hours/) |
| 3 Palms Action Sports Park | motocross track | Conroe, TX | 30.24415, -95.46281 | openstreetmap_cross_checked | [1](https://www.openstreetmap.org/node/9284606377) [2](https://www.threepalmsesp.com/contact) |
| Cycle Ranch Motocross Park | motocross track | Floresville, TX | 29.142134, -98.1114 | place_listing_cross_checked | [1](https://cycleranchmx.com/pages/contact-us) [2](https://exa.ai/library/place/pbbpqr1kj9d) |
| Murphy's MX | motocross track | Red Rock, TX | 29.940622, -97.475 | place_listing_cross_checked | [1](https://www.murphysmx.com/) [2](https://exa.ai/library/place/jlwlpj7nf2h) |
| Swan MX | motocross track | Tyler, TX | 32.44289, -95.40209 | openstreetmap_cross_checked | [1](https://www.openstreetmap.org/way/1220668653) [2](https://swanmx.com/contact/) |
| Freestone Raceway | motocross track | Wortham, TX | 31.805522, -96.400399 | official_venue_source | [1](https://freestonemx.com/directions/) |
| Onion Creek Soccer Complex | soccer complex | Austin, TX | 30.17531, -97.74092 | openstreetmap_cross_checked | [1](https://www.openstreetmap.org/way/904330963) [2](https://austinmenssoccer.com/fields/ocsc/) |
| Baytown Soccer Park | soccer complex | Baytown, TX | 29.77383, -95.00343 | openstreetmap_cross_checked | [1](https://www.openstreetmap.org/way/1210330484) [2](https://baytown.org/facilities/facility/details/Baytown-Soccer-Park-9) |
| MoneyGram Soccer Park at Elm Fork | soccer complex | Dallas, TX | 32.879, -96.90863 | openstreetmap_cross_checked | [1](https://www.openstreetmap.org/way/471913943) [2](https://www.dallasparks.org/facilities/facility/details/MoneyGram-Soccer-Park-at-Elm-Fork-762) |
| Ross Stewart Soccer Complex | soccer complex | Farmers Branch, TX | 32.92231, -96.92298 | openstreetmap_cross_checked | [1](https://www.openstreetmap.org/way/839492552) [2](https://www.dallassports.org/directory/ross-stewart-soccer-complex/) [3](https://dallastexans.sportngin.com/page/show/908231-ross-stewart-soccer-complex) |
| Irving Soccer Complex | soccer complex | Irving, TX | 32.88244, -96.99613 | openstreetmap_cross_checked | [1](https://www.openstreetmap.org/way/646994549) [2](https://irvingtx.gov/index.php?project=37&projectsection=13&section=park-facilities) |
| Philip Thompson Soccer Complex | soccer complex | Mansfield, TX | 32.58596, -97.08482 | openstreetmap_cross_checked | [1](https://www.openstreetmap.org/way/352860612) [2](https://www.mansfieldtexas.gov/Facilities/Facility/Details/Philip-Thompson-Soccer-Complex-31) |
| Al Ruschhaupt Soccer Complex | soccer complex | McKinney, TX | 33.18985, -96.64898 | openstreetmap_cross_checked | [1](https://www.openstreetmap.org/way/487447235) [2](https://parks.mckinneytexas.org/park/al-ruschhaupt-soccer-complex/) |
| McKinney Soccer Complex at Craig Ranch | soccer complex | McKinney, TX | 33.14183, -96.69959 | openstreetmap_cross_checked | [1](https://www.openstreetmap.org/way/449759755) [2](https://parks.mckinneytexas.org/park/mckinney-soccer-complex-at-craig-ranch/) |
| HEB Soccer Complex | soccer complex | New Braunfels, TX | 29.69128, -98.14429 | openstreetmap_cross_checked | [1](https://www.openstreetmap.org/way/541510107) [2](https://newbraunfels.gov/facilities/facility/details/HEB-Soccer-Complex-17) |
| GVTC Soccer Complex | soccer complex | San Antonio, TX | 29.72688, -98.44906 | openstreetmap_cross_checked | [1](https://www.openstreetmap.org/way/373176746) [2](https://www.saunitedsoccer.com/contact) [3](https://maps.apple.com/place?place-id=IF6BD1863BFC1E3AB) |
| Mission City Soccer Complex | soccer complex | San Antonio, TX | 29.21291, -98.45203 | openstreetmap_cross_checked | [1](https://www.openstreetmap.org/way/320564912) [2](https://www.missioncitysoccer.com/mission-city-soccer-complex-location) |
| Schertz Soccer Complex | soccer complex | Schertz, TX | 29.56661, -98.2895 | openstreetmap_cross_checked | [1](https://www.openstreetmap.org/way/489849710) [2](http://www.schertzsoccercomplex.org/complex-location.html) |

The 35 legacy Texas ball fields keep `coordinates_source.method = "legacy"`.
