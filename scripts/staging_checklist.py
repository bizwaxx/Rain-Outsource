#!/usr/bin/env python3
"""Pre-launch staging checklist: GREEN LIGHT or a punch list.

    python scripts/staging_checklist.py                      # production, sampled
    python scripts/staging_checklist.py --mode full          # every confirmed venue + offline pass over every map venue
    python scripts/staging_checklist.py --base https://<preview>.vercel.app --label "PR #37"
    python scripts/staging_checklist.py --known-previews     # production + the open PR previews
    make staging                                             # same as the first line

Exit code 0 = GREEN LIGHT, 1 = punch list, 2 = the checklist itself crashed.
See docs/staging-checklist.md.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

KNOWN_PREVIEWS = {
    "PR #31 construction go_no_go": "https://rainout-agent-source-git-feat-construc-12303a-bizwax-s-projects.vercel.app",
    "PR #33 landing/SEO": "https://rainout-agent-source-git-growth-landing-seo-bizwax-s-projects.vercel.app",
    "PR #35 should_we_cancel": "https://rainout-agent-source-git-feat-should-w-7c3581-bizwax-s-projects.vercel.app",
    "PR #37 switch page": "https://rainout-agent-source-git-growth-switch-page-bizwax-s-projects.vercel.app",
    "PR #27 Stripe (test mode)": "https://rainout-agent-source-git-monetize-b-stripe-bizwax-s-projects.vercel.app",
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=None, help="deployment to test (default: production)")
    ap.add_argument("--label", default=None, help="name for --base in the report (default: production / preview)")
    ap.add_argument("--preview", action="append", default=[], metavar="LABEL=URL", help="extra preview(s) to test")
    ap.add_argument("--known-previews", action="store_true", help="also test the open PR previews listed in this script")
    ap.add_argument("--mode", choices=["sample", "full"], default="sample")
    ap.add_argument("--skip-live", action="store_true")
    ap.add_argument("--skip-backtest", action="store_true")
    ap.add_argument("--code-path", default=None, help="import rainout_agent from this checkout for the backtest (e.g. the construction branch)")
    ap.add_argument("--allow-prod-test-report", action="store_true", help="also write a marked, expiring test report on production")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--out", default="staging-checklist-results.md")
    ap.add_argument("--json", default=None, help="also write raw results as JSON")
    a = ap.parse_args(argv)

    if a.code_path:
        sys.path.insert(0, str(Path(a.code_path).resolve()))
    sys.path.insert(1 if a.code_path else 0, str(ROOT))
    from staging import checks as C
    from staging import report as R

    targets = [{"label": a.label or ("production" if not a.base or a.base.rstrip("/") == C.PRODUCTION else "preview"),
                "base": (a.base or C.PRODUCTION).rstrip("/"),
                "kind": "production" if not a.base or a.base.rstrip("/") == C.PRODUCTION else "preview"}]
    previews = dict(KNOWN_PREVIEWS) if a.known_previews else {}
    for p in a.preview:
        k, _, v = p.partition("=")
        previews[k] = v
    targets += [{"label": k, "base": v.rstrip("/"), "kind": "preview"} for k, v in previews.items()]

    rows: list[dict] = []
    if not a.skip_live:
        main_tools = list(C.CORE_MCP_TOOLS)
        for tg in targets:
            print(f"== {tg['label']} ({tg['base']})", file=sys.stderr)
            t = C.Target(tg["base"], tg["label"], tg["kind"])
            run_target(C, t, a, main_tools, full=(tg is targets[0]))
            rows += [c.row() for c in t.checks]
        if a.mode == "full":
            rows.append(C.osm_integrity().row())
    bt = None
    if not a.skip_backtest:
        from staging import backtest as B
        print("== backtest", file=sys.stderr)
        bt = B.run(log=lambda s: print("  " + s, file=sys.stderr))
    meta = {"mode": a.mode, "targets": targets}
    md = R.render(meta, rows, bt)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(md)
    if a.json:
        Path(a.json).write_text(json.dumps({"meta": meta, "checks": rows, "backtest": bt}, indent=1, default=str))
    v, punch, _ = R.verdict(rows, bt)
    print(f"\n{v}" + (f": {len(punch)} item(s)" if punch else ""))
    for p in punch:
        print(f"  - [{p['target']}] {p['id']}: {p['detail'][:160]}")
    print(f"report: {a.out}")
    return 0 if v == "GREEN LIGHT" else 1


def run_target(C, t, a, main_tools: list[str], full: bool) -> None:
    """All checks for one deployment. Features that aren't deployed there (404) are skipped."""
    def has(path: str) -> bool:
        return t.get(path).status not in (404, 0)

    C.check_core(t)
    fields = C.list_confirmed(t)
    is_primary_prod = t.kind == "production"
    # Previews: a smaller venue sample (one per official source) keeps NWS traffic down.
    C.check_status_venues(t, fields, a.mode if is_primary_prod else "preview", workers=a.workers)
    if is_primary_prod or full:
        C.check_osm_sample(t, a.mode, workers=a.workers)
        C.check_tracks(t)
        C.check_teams_and_schedule(t)
        C.check_rain(t)
        C.check_providers(t)
    expect = list(main_tools)
    has_gng = has("/v1/go-no-go?activity=softball&venue=Krieg")
    has_swc = has("/v1/should-we-cancel?activity=softball&venue=Krieg")
    if has_gng:
        expect += ["go_no_go", "check_concrete_pour", "check_materials_risk", "get_precipitation_nowcast"]
    if has_swc:
        expect += ["should_we_cancel"]
    C.check_mcp(t, expect)
    # One marked test report per run: on the first preview tested (previews share one store), or on
    # production only with --allow-prod-test-report.
    allow = (t.kind == "preview" and not getattr(a, "_report_written", False)) or (t.kind == "production" and a.allow_prod_test_report)
    C.check_reports(t, "Havins", allow_write=allow)
    if allow and t.kind == "preview":
        a._report_written = True
    if has_gng:
        C.check_construction(t)
    if has_swc:
        C.check_should_we_cancel(t)
    if t.get("/v1/admin/usage").status != 404:
        C.check_usage(t)
    if has("/.well-known/mcp/server-card"):
        C.check_landing(t)
    if has("/switch.js"):
        C.check_switch(t)
    if t.get("/v1/billing/webhook", method="POST", data=b"{}", headers={"Content-Type": "application/json"}).status != 404:
        C.check_billing_webhook(t)
    if is_primary_prod:
        for ch in ("Email alerts", "Push notifications", "Odds-change webhooks"):
            t.checks.append(C.Check(f"not_built:{ch}", "alerts", t.label, "not_built", f"{ch}: not built anywhere (no code on main or any open PR)",
                                    owner="Jimmy (product call)", fix="Launch without it and say so, or build it before launch.", severity="known_gap"))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(2)
