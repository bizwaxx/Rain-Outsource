from rainout_agent import event_risk as er
from rainout_agent import venue_profile as vp
from rainout_agent.status_api import build_status_result

WX = {"rain_chance_percent": 10, "thunderstorm_likely": False, "source": "NWS test", "last_checked": "2026-06-07T12:00:00-05:00"}


def test_defaults_never_guess_maintained():
    p = vp.build({"sport": "softball", "surface": "unknown"})
    assert (p["maintenance"], p["level"], p["surface"], p["maintained"]) == ("unknown", "rec", "unknown", False)
    assert p["sources"]["maintenance"] == vp.NO_EVIDENCE


def test_catalog_block_needs_a_source():
    no_src = vp.build({"venue_profile": {"maintenance": "yes", "level": "college"}})
    assert not no_src["maintained"]
    with_src = vp.build({"venue_profile": {"level": "college", "level_source": "https://example.edu/ballpark"}})
    assert with_src["maintained"] and with_src["level"] == "college_pro"


def test_osm_surface_tag_is_used_with_its_source():
    p = vp.build({"osm": {"surface": {"value": "turf", "tag": "artificial_turf", "element": "https://www.openstreetmap.org/way/1"}}})
    assert p["surface"] == "turf" and "artificial_turf" in p["sources"]["surface"] and not p["maintained"]


def test_league_override_and_bad_values():
    ov = vp.parse_override({"venue_maintenance": ["yes"], "venue_surface": "plastic"})
    p = vp.build({}, ov)
    assert p["maintained"] and p["override_applied"] and "unverified" in p["sources"]["maintenance"]
    assert "venue_surface" in p["override_errors"]


def test_maintained_soak_counts_much_less():
    feat = {"earlier_today_in": 0.6, "prior_24h_in": 0.4, "prior_48h_in": 0.0, "window_pop": 10}
    p = {**er.PARAMS, "maint_soak_factor": 0.35}
    rec = er.adjust(90, feat, "slow", p, True, maintained=False)["play_probability_percent"]
    kept = er.adjust(90, feat, "slow", p, True, maintained=True)["play_probability_percent"]
    assert kept > rec


def test_grass_ball_field_keeps_dirt_infield_drainage():
    f = {"sport": "baseball", "venue_profile_resolved": {"surface": "grass"}}
    assert er.drainage_class(f) == "slow"
    assert er.drainage_class({"sport": "soccer", "venue_profile_resolved": {"surface": "grass"}}) == "medium"
    assert er.drainage_class({"sport": "baseball", "venue_profile_resolved": {"surface": "turf"}}) == "fast"


def test_status_exposes_profile_and_override():
    r = build_status_result("Downs", "2026-06-07T19:00:00-05:00", weather=dict(WX), official_status="unknown")
    assert r["venue_profile"]["level"] == "rec" and r["venue_profile"]["maintained"] is False
    r = build_status_result("Downs", "2026-06-07T19:00:00-05:00", weather=dict(WX), official_status="unknown",
                            venue_override={"maintenance": "yes"})
    assert r["venue_profile"]["maintained"] is True and r["venue_profile"]["override_applied"]
