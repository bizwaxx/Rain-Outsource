import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from api.site import handler as SiteHandler
from tests.test_site_pages import run

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"


@pytest.mark.parametrize("path,ctype", [
    ("/switch", "text/html"), ("/switch/", "text/html"), ("/switch.html", "text/html"),
    ("/switch/embed", "text/html"), ("/switch.js", "application/javascript"), ("/switch-embed.js", "application/javascript"),
])
def test_switch_assets_are_served(path, ctype):
    status, headers, body = run(SiteHandler, path)
    assert status == 200 and headers["Content-Type"].startswith(ctype) and body


def test_vercel_routes_switch_paths_to_site_handler():
    routes = json.loads((ROOT / "vercel.json").read_text())["routes"]

    def dest(path):
        return next((r["dest"] for r in routes if re.fullmatch(r["src"], path)), None)

    for path in ("/switch", "/switch/", "/switch/embed", "/switch.js", "/switch-embed.js"):
        assert dest(path) == "/api/site.py", path


def test_switch_page_content_and_honest_stubs():
    html = (SITE / "switch.html").read_text()
    assert "<title>Switch free in 5 minutes" in html
    assert 'name="description"' in html and 'rel="canonical"' in html
    assert "Keep your current line, add ours on top" in html
    # each alert channel is labelled as working or stub
    assert html.count('class="pill works"') == 2 and html.count('class="pill stub"') == 2
    assert "We don't collect any email addresses" in html
    assert "<form" not in html  # nothing on the page submits personal data
    assert "isn't affiliated" in html
    for credit in ("National Weather Service", "Open-Meteo", "OpenStreetMap", "ODbL"):
        assert credit in html


def test_switch_page_uses_only_existing_public_endpoints():
    js = (SITE / "switch.js").read_text() + (SITE / "switch-embed.js").read_text()
    used = set(re.findall(r'"(/v1/[a-z/]+)', js))
    assert used <= {"/v1/fields", "/v1/status", "/v1/reports", "/v1/schedule/check", "/v1/outcomes"}, used
    assert "innerHTML" not in js  # all API text is rendered with textContent
    assert "http://" not in js.replace('"https://"', "")


def test_no_competitor_logos_or_assets():
    for f in SITE.iterdir():
        assert not re.search(r"rainedout|rainoutline|statusfy", f.name, re.I)
    html = (SITE / "switch.html").read_text()
    assert not re.search(r'<img[^>]+(rainedout|rainoutline)', html, re.I)


def test_sitemap_and_landing_link_switch_page():
    assert "https://rainout-agent-source.vercel.app/switch</loc>" in (SITE / "sitemap.xml").read_text()
    assert 'href="/switch"' in (SITE / "index.html").read_text()


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_field_list_parser_in_js():
    script = (SITE / "switch.js").read_text()
    harness = """
const window = {}; const document = {addEventListener(){}};
%s
const P = window.RainSwitch.parseFieldList;
const out = {
  lines: P("Krieg Field, Austin, TX\\nZilker Metropolitan Park, TX\\n30.2672,-97.7431 City Park Field 3\\n\\n"),
  csv: P("name,city,state\\n\\"Field, One\\",Austin,tx\\nHavins,,TX\\n"),
  iso: window.RainSwitch.isoWithOffset("2026-10-23", "19:00"),
};
console.log(JSON.stringify(out));
""" % script
    res = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=30, env={"TZ": "America/Chicago"})
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    lines = out["lines"]
    assert [x["name"] for x in lines] == ["Krieg Field", "Zilker Metropolitan Park", "City Park Field 3"]
    assert lines[0]["state"] == "TX" and lines[0]["city"] == "Austin"
    assert lines[2]["lat"] == 30.2672 and lines[2]["lon"] == -97.7431
    assert [(x["name"], x["state"]) for x in out["csv"]] == [("Field, One", "TX"), ("Havins", "TX")]
    assert out["iso"] == "2026-10-23T19:00:00-05:00"


def test_was_it_on_buttons_confirm_before_posting():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "site"
    for name in ("switch.js", "switch-embed.js"):
        js = (root / name).read_text()
        assert '"/v1/outcomes"' in js and "window.confirm(" in js and "Was it on?" in js
