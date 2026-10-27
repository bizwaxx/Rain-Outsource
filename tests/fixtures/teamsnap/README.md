Mock TeamSnap API v3 responses (TEST DATA, not a real team). Shaped like
`GET https://api.teamsnap.com/v3/{teams,events,locations,opponents}/search?team_id=...`
Collection+JSON responses: `collection.items[].data[] = {name, value}`.
Field names follow the APIv3 event template (is_game, is_canceled, start_date,
location_id, opponent_id, game_type, duration_in_minutes, time_zone_iana_name).
