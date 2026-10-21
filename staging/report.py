"""Verdict + markdown report for the staging checklist."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from staging.checks import THRESHOLDS

BLOCKING = {"blocker", "should_fix"}


def backtest_items(bt: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Punch-list rows from the backtest."""
    if not bt:
        return []
    items = []
    s = dict(bt["summary"]["overall"])
    held = (bt["summary"].get("held_out") or {}).get("leave_one_out")
    if held:
        # Gate on the honest (leave-one-out) numbers for the calibrated model; construction keeps its in-sample grade.
        cons = bt["summary"]["by_field_type"].get("construction") or {"confident_wrong": 0}
        s["confident_wrong"] = held["confident_wrong"] + cons["confident_wrong"]
        s["directional_accuracy"] = held["directional_accuracy"]
    if s["confident_wrong"] > THRESHOLDS["backtest_confident_wrong"]:
        ids = held["confident_wrong_ids"] if held else [r["id"] for r in bt["results"] if r.get("confident_wrong")]
        items.append({"id": "backtest:confident_wrong", "target": "Backtest (offline, production model code)", "status": "fail", "severity": "blocker",
                      "detail": f"{s['confident_wrong']} confident wrong calls" + (" (leave-one-out)" if held else "") + ": " + ", ".join(ids),
                      "owner": "Model (event_risk.py) / Jimmy (accept or not)",
                      "fix": "Soak, storm-window, wind, cold and venue-maintenance rules are in (event_risk v2). Check each listed call: "
                             "events cancelled for a forecast that never happened are unpredictable from archived weather (Jimmy decides "
                             "whether to accept them); the rest need more real fixtures or venue data, not new rules. Never tune on the "
                             "held-out score."})
    if s["directional_accuracy"] is not None and s["directional_accuracy"] < THRESHOLDS["backtest_directional_accuracy"]:
        items.append({"id": "backtest:accuracy", "target": "Backtest (offline, production model code)", "status": "fail", "severity": "blocker",
                      "detail": f"directional accuracy {s['directional_accuracy']}%" + (" (leave-one-out)" if held else "") + f" (need >= {THRESHOLDS['backtest_directional_accuracy']}%)",
                      "owner": "Model", "fix": "See the per-type table and mismatch list."})
    bad_beh = [(r["id"], b) for r in bt["results"] for b in r.get("behaviour", []) if b.get("ok") is False]
    parser = [x for x in bad_beh if x[1]["check"] == "mx_notice_parser"]
    other = [x for x in bad_beh if x[1]["check"] != "mx_notice_parser"]
    if parser:
        items.append({"id": "backtest:mx_notice_parser", "target": "Backtest (offline, production parser code)", "status": "fail", "severity": "should_fix",
                      "detail": "real cancellation posts the motocross notice parser would miss: " + "; ".join(f"{i}: {b['detail']}" for i, b in parser),
                      "owner": "Parsers (motocross.official_status)",
                      "fix": "motocross.official_status only looks at the FIRST date in a post, so a dateline ('November 1, 2013 "
                             "(Wortham, Texas) -') or a reschedule date ('make-up on July 8') hides the race date. Check every date in "
                             "the post, ignore dates after 'rescheduled to/for' or 'make-up', and count practice notices when the event is "
                             "a practice. Simply stripping the dateline was tried and broke a real 'Tonight's race rained out' post, so "
                             "this needs its own PR with the real posts as fixtures."})
    if other:
        items.append({"id": "backtest:behaviour", "target": "Backtest", "status": "fail", "severity": "blocker",
                      "detail": "; ".join(f"{i}: {b['check']} {b['detail']}" for i, b in other), "owner": "Model", "fix": "Fix status/report precedence."})
    if not bt["precedence"]["ok"]:
        items.append({"id": "backtest:report_precedence", "target": "Backtest", "status": "fail", "severity": "blocker",
                      "detail": bt["precedence"]["detail"], "owner": "API/backend (reports.py)", "fix": "Official status must win over reports; reports expire."})
    return items


def verdict(check_rows: list[dict[str, Any]], bt: dict[str, Any] | None) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    punch = [c for c in check_rows if c["status"] in ("fail", "warn") and c["severity"] in BLOCKING]
    punch += backtest_items(bt)
    gaps = [c for c in check_rows if c["status"] == "not_built"]
    # one row per gap, not per target
    seen, uniq = set(), []
    for g in gaps:
        if g["id"] not in seen:
            seen.add(g["id"])
            uniq.append(g)
    return ("GREEN LIGHT" if not punch else "PUNCH LIST"), punch, uniq


def _cell(s: Any) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def render(meta: dict[str, Any], check_rows: list[dict[str, Any]], bt: dict[str, Any] | None) -> str:
    v, punch, gaps = verdict(check_rows, bt)
    now = datetime.now(ZoneInfo("America/Chicago")).strftime("%Y-%m-%d %H:%M CT")
    L = [f"# Rain-Outsource staging checklist: **{v}**", "",
         f"Run {now} · mode `{meta['mode']}` · targets: " + ", ".join(f"{t['label']} ({t['kind']}: {t['base']})" for t in meta["targets"]), ""]
    if v == "GREEN LIGHT":
        L.append("All blocking checks passed and the backtest met its thresholds.")
    else:
        L.append(f"{len(punch)} item(s) to fix before launch. Production rows are marked **production**; preview rows name the PR.")
    L += ["", "## Punch list", "", "| # | Item | Where | Owner | Fix |", "|---|---|---|---|---|"]
    for i, p in enumerate(sorted(punch, key=lambda x: (x["severity"] != "blocker", x["target"])), 1):
        L.append(f"| {i} | **{_cell(p['id'])}** ({p['severity']}): {_cell(p['detail'])[:400]} | {_cell(p['target'])} | {_cell(p['owner'])} | {_cell(p['fix'])} |")
    if gaps:
        L += ["", "### Known gaps (not built; not counted as failures)", ""]
        for g in gaps:
            L.append(f"- **{g['id'].split(':', 1)[1]}**: {g['detail']}. Owner: {g['owner']}. {g['fix']}")
    L += ["", "## Thresholds", "", "| Threshold | Value |", "|---|---|"]
    L += [f"| {k} | {v_} |" for k, v_ in THRESHOLDS.items()]
    L += ["", "Pass rules: every endpoint 200; official source checks readable for at least the rate above; no official check older than "
          f"{THRESHOLDS['official_check_age_min']} min and no weather older than {THRESHOLDS['weather_age_min']} min; parsers return valid data; "
          f"backtest directional accuracy >= {THRESHOLDS['backtest_directional_accuracy']}% with zero confident wrong calls "
          "(>= 80% play odds on an event that was rained out, or <= 20% on one that was played)."]
    if bt:
        L += render_backtest(bt)
    L += ["", "## All checks", ""]
    for target in meta["targets"]:
        rows = [c for c in check_rows if c["target"] == target["label"]]
        if not rows:
            continue
        L += [f"### {target['label']} ({target['kind']}) `{target['base']}`", "", "| Status | Check | Detail |", "|---|---|---|"]
        for c in rows:
            sev = f" ({c['severity']})" if c["status"] in ("fail", "warn") and c["severity"] else ""
            L.append(f"| {c['status'].upper()}{sev} | {_cell(c['id'])} | {_cell(c['detail'])[:500]} |")
        L.append("")
    other = [c for c in check_rows if c["target"] not in {t["label"] for t in meta["targets"]}]
    if other:
        L += ["### Offline", "", "| Status | Check | Detail |", "|---|---|---|"]
        L += [f"| {c['status'].upper()} | {_cell(c['id'])} | {_cell(c['detail'])[:500]} |" for c in other]
    return "\n".join(L) + "\n"


def render_backtest(bt: dict[str, Any]) -> list[str]:
    s = bt["summary"]
    L = ["", "## Known-outcome backtest", "",
         "Real past events with a public outcome, replayed through the production model code with that day's archived weather "
         "(official status set to unknown, i.e. the model alone). Bands: >= 70% go, 40-69% wait (hedged), < 40% no_go. "
         "'Directional' splits at 50%.", "",
         "| Field type | Fixtures | Played / not | Correct | Hedged | Wrong | Directional | Confident wrong |", "|---|---|---|---|---|---|---|---|"]
    for k, a in list(s["by_field_type"].items()) + [("**all**", s["overall"])]:
        L.append(f"| {k} | {a['fixtures']} | {a['played']} / {a['not_played']} | {a['correct']} | {a['hedged']} | {a['wrong']} | "
                 f"{a['directional_accuracy']}% | {a['confident_wrong']} |" + (f" ({a['skipped']} skipped)" if a["skipped"] else ""))
    h = s.get("held_out")
    if h:
        lo, ins = h["leave_one_out"], h["in_sample"]
        L += ["", "Before (v1, start-hour model) vs after (v2 event_risk). In-sample uses the parameters fitted on all fixtures; "
              "**held-out is leave-one-out** (each fixture predicted with parameters fitted without it) and is the honest number.", "",
              "| Field type | v1 directional | v1 confident wrong | v2 in-sample directional | v2 in-sample CW | v2 held-out directional | v2 held-out CW |",
              "|---|---|---|---|---|---|---|"]
        for k in list(lo["by_field_type"]) + ["**all**"]:
            v1 = s["v1_overall"] if k == "**all**" else s["v1_by_field_type"].get(k, {})
            a = ins if k == "**all**" else ins["by_field_type"][k]
            b = lo if k == "**all**" else lo["by_field_type"][k]
            L.append(f"| {k} | {v1.get('directional_accuracy')}% | {v1.get('confident_wrong')} | {a['directional_accuracy']}% | {a['confident_wrong']} | "
                     f"{b['directional_accuracy']}% | {b['confident_wrong']} |")
        L += ["", f"Held-out confident wrong: {', '.join(lo['confident_wrong_ids']) or 'none'}. Chosen parameters: `{h['chosen']}` "
              f"({h['grid_points']} grid points; {h['loo_distinct_choices']} distinct choices across the leave-one-out folds)."]
    L += ["", "By weather grade (archived_forecast is the fair test; the proxies convert rain amounts to a rain chance):", "",
          "| Weather grade | Fixtures | Directional | Confident wrong |", "|---|---|---|---|"]
    for k, a in s["by_weather_grade"].items():
        L.append(f"| {k} | {a['fixtures']} | {a['directional_accuracy']}% | {a['confident_wrong']} |")
    L += ["", f"Report handling: {bt['precedence']['detail']} ({'ok' if bt['precedence']['ok'] else 'FAIL'}).", "",
          "### Every fixture", "", "| Fixture | Type | Outcome | Model | Call | Result | Weather | Notes | Source |", "|---|---|---|---|---|---|---|---|---|"]
    for r in bt["results"]:
        wx = f"{r.get('weather_grade')}, rain {r.get('rain_chance_percent')}%" + (", storms" if r.get("thunderstorm_likely") else "")
        if r.get("soak_index_in") is not None:
            wx += f", soak {r['soak_index_in']} in"
        notes = "; ".join([b["detail"] for b in r.get("behaviour", []) if b["check"] == "mx_notice_parser"] + r.get("assumptions", []))
        res = r["result"] + (" (CONFIDENT WRONG)" if r.get("confident_wrong") else "")
        L.append(f"| {r['id']} | {r['field_type']} | {r['outcome']}: {_cell(r['cause'])} | {r.get('p')}% | {r.get('decision')} | {res} | {wx} | {_cell(notes)} | {r['source_url']} |")
    L += ["", "Mismatches (wrong or hedged): " + (", ".join(s["mismatches"]) or "none"), "", "### Left out (could not verify)", ""]
    L += [f"- {e['event']}: {e['reason']}" for e in bt.get("excluded", [])]
    return L
