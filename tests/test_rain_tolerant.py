from rainout_agent import event_risk as er


def test_rain_tolerant_only_college_pro_soccer():
    assert er.rain_tolerant("soccer", "college_pro")
    assert not er.rain_tolerant("soccer", "rec")
    assert not er.rain_tolerant("baseball", "college_pro")


def test_rule_off_by_default_changes_nothing():
    feat = {"window_pop": 95, "earlier_today_in": 1.0, "prior_24h_in": 0.5, "prior_48h_in": 0.0}
    assert er.PARAMS["pro_rain_factor"] is None and er.PARAMS["pro_soak_factor"] is None
    a = er.adjust(er.base_percent(95, "medium"), feat, "medium")
    b = er.adjust(er.base_percent(95, "medium", tolerant=True), feat, "medium", tolerant=True)
    assert a["play_probability_percent"] == b["play_probability_percent"]


def test_rule_on_raises_rain_only_odds():
    p = {**er.PARAMS, "pro_rain_factor": 0.0, "pro_soak_factor": 0.0}
    feat = {"window_pop": 95, "earlier_today_in": 1.0, "prior_24h_in": 0.5, "prior_48h_in": 0.0}
    off = er.adjust(er.base_percent(95, "medium", p), feat, "medium", p)["play_probability_percent"]
    on = er.adjust(er.base_percent(95, "medium", p, True), feat, "medium", p, tolerant=True)
    assert on["play_probability_percent"] > off and er.RAIN_TOLERANT_NOTE in on["reasons"]
