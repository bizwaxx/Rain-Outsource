import io
import json
from pathlib import Path

import pytest

from api.index import handler as IndexHandler
from api.site import handler as SiteHandler
from rainout_agent import http_app, site_pages
from rainout_agent.mcp_server import SERVER_VERSION, TOOLS

ROOT = Path(__file__).resolve().parents[1]
BROWSER = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"


def run(handler_class, path, accept=None, method="GET"):
    inst = handler_class.__new__(handler_class)
    inst.path = path
    inst.headers = {"Accept": accept} if accept is not None else {}
    inst.wfile = io.BytesIO()
    out = {"headers": {}}
    inst.send_response = lambda code, message=None: out.__setitem__("status", code)
    inst.send_header = lambda k, v: out["headers"].__setitem__(k, v)
    inst.end_headers = lambda: None
    getattr(inst, f"do_{method}")()
    return out["status"], out["headers"], inst.wfile.getvalue()


@pytest.mark.parametrize("accept", [None, "", "*/*", "application/json", "application/json, text/html",
                                    "text/html;q=0.5, application/json"])
def test_root_keeps_json_for_agents(accept):
    status, headers, body = run(IndexHandler, "/", accept)
    assert status == 200
    assert headers["Content-Type"] == "application/json"
    assert headers["Vary"] == "Accept"
    payload = json.loads(body)
    assert payload["service"] == "rainout-source"
    assert payload["openapi_url"].endswith("/openapi.yaml")
    assert payload["supported_fields"]


def test_root_serves_landing_page_to_browsers():
    status, headers, body = run(IndexHandler, "/", BROWSER)
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert headers["Vary"] == "Accept"
    assert "s-maxage" not in headers["Cache-Control"]  # never let the edge hand HTML to a JSON client
    html = body.decode()
    assert "<title>Rain-Outsource" in html
    assert 'name="description"' in html and 'property="og:image"' in html
    assert "ALL AI agents and ALL humans, worldwide" in html
    ld = json.loads(html.split('<script type="application/ld+json">')[1].split("</script>")[0])
    types = {node["@type"] for node in ld["@graph"]}
    assert types == {"SoftwareApplication", "WebAPI"}
    assert ld["@graph"][0]["softwareVersion"] == SERVER_VERSION
    for credit in ("National Weather Service", "NOAA", "Open-Meteo", "OpenStreetMap", "ODbL"):
        assert credit in html


def test_format_query_overrides_accept():
    assert run(IndexHandler, "/?format=json", BROWSER)[1]["Content-Type"] == "application/json"
    assert run(IndexHandler, "/?format=html", "application/json")[1]["Content-Type"].startswith("text/html")


def test_api_path_is_always_json_and_edge_cached():
    status, headers, body = run(IndexHandler, "/api", BROWSER)
    assert headers["Content-Type"] == "application/json"
    assert "s-maxage" in headers["Cache-Control"]


def test_head_requests_have_no_body():
    status, headers, body = run(IndexHandler, "/", BROWSER, method="HEAD")
    assert status == 200 and body == b"" and int(headers["Content-Length"]) > 0
    status, headers, body = run(SiteHandler, "/icon.png", method="HEAD")
    assert status == 200 and body == b""


@pytest.mark.parametrize("path,ctype", [
    ("/icon.svg", "image/svg+xml"), ("/icon.png", "image/png"), ("/favicon.ico", "image/x-icon"),
    ("/icon-400.png", "image/png"), ("/robots.txt", "text/plain"), ("/sitemap.xml", "application/xml"),
    ("/privacy", "text/html"), ("/terms", "text/html"), ("/privacy.html", "text/html"), ("/terms/", "text/html"),
])
def test_static_discovery_files(path, ctype):
    status, headers, body = run(SiteHandler, path)
    assert status == 200
    assert headers["Content-Type"].startswith(ctype)
    assert body


def test_png_icons_have_expected_sizes():
    import struct

    for name, size in (("icon.png", 512), ("icon-400.png", 400)):
        data = (ROOT / "site" / name).read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        assert struct.unpack(">II", data[16:24]) == (size, size)


def test_privacy_and_terms_are_marked_draft():
    for path in ("/privacy", "/terms"):
        html = run(SiteHandler, path)[2].decode()
        assert "DRAFT FOR JIMMY'S REVIEW" in html
        assert "github.com/bizwaxx/Rain-Outsource/issues" in html
    assert "not safety or engineering advice" in run(SiteHandler, "/terms")[2].decode().lower()


def test_robots_points_to_sitemap():
    body = run(SiteHandler, "/robots.txt")[2].decode()
    assert "Sitemap: https://rainout-agent-source.vercel.app/sitemap.xml" in body


def test_server_card_matches_live_tool_list():
    status, headers, body = run(SiteHandler, "/.well-known/mcp/server-card.json")
    assert status == 200 and headers["Content-Type"] == "application/json"
    card = json.loads(body)
    assert card["serverInfo"] == {"name": "rainout-source", "title": "Rain-Outsource", "version": SERVER_VERSION}
    assert card["authentication"] == {"required": False}
    assert [t["name"] for t in card["tools"]] == [t["name"] for t in TOOLS]
    assert all("annotations" in t and "title" in t for t in card["tools"])


def test_unknown_site_path_is_404():
    assert run(SiteHandler, "/nope")[0] == 404


def test_registry_files_match_live_version():
    server = json.loads((ROOT / "server.json").read_text())
    assert server["name"] == "io.github.bizwaxx/rain-outsource"
    assert server["version"] == SERVER_VERSION
    assert len(server["description"]) <= 100
    assert server["remotes"] == [{"type": "streamable-http", "url": "https://rainout-agent-source.vercel.app/mcp"}]
    glama = json.loads((ROOT / "glama.json").read_text())
    assert glama["maintainers"] == ["bizwaxx"]


def test_vercel_routes_cover_discovery_paths():
    import re

    routes = json.loads((ROOT / "vercel.json").read_text())["routes"]

    def dest(path):
        for r in routes:
            if re.fullmatch(r["src"], path):
                return r["dest"]

    for path in ("/icon.svg", "/favicon.ico", "/robots.txt", "/sitemap.xml"):
        assert dest(path) == "/api/site.py", path
    # Free launch: #51 serves the privacy/terms pages, icon.png and the server card from the index function.
    for path in ("/icon.png", "/privacy", "/terms", "/.well-known/mcp/server-card.json"):
        assert dest(path) == "/api/index.py", path
    assert dest("/") == "/api/index.py"
    assert dest("/mcp") == "/api/mcp.py"


def test_wants_html_rules():
    assert site_pages.wants_html(BROWSER) is True
    assert site_pages.wants_html("*/*") is False
    assert site_pages.wants_html(None) is False
    assert site_pages.wants_html("application/json;q=0.9, text/html") is True


def _wsgi(path, accept=""):
    env = {"REQUEST_METHOD": "GET", "PATH_INFO": path, "QUERY_STRING": "", "HTTP_ACCEPT": accept}
    seen = {}
    body = b"".join(http_app.application(env, lambda s, h: seen.update(status=s, headers=dict(h))))
    return seen["status"], seen["headers"], body


def test_local_wsgi_app_serves_same_pages():
    status, headers, body = _wsgi("/", BROWSER)
    assert status.startswith("200") and headers["Content-Type"].startswith("text/html")
    status, headers, body = _wsgi("/.well-known/mcp/server-card.json")
    assert json.loads(body)["serverInfo"]["version"] == SERVER_VERSION


def test_site_pages_have_bounded_metrics_labels():
    from rainout_agent import metrics

    assert metrics.endpoint_for("/.well-known/mcp/server-card.json") == "server_card"
    assert metrics.endpoint_for("/privacy") == "privacy"
    assert metrics.endpoint_for("/icon.png") == "icon"
    assert getattr(SiteHandler.do_GET, "__name__") == "do_GET"


def test_privacy_describes_keyed_rate_limit_hash_accurately():
    import hashlib

    from rainout_agent import reports

    from rainout_agent.site_pages import SITE_DIR

    html = (SITE_DIR / "privacy.html").read_text()
    assert "keyed one-way hash" in html and "changes every day" in html and "within an hour" in html
    assert "We can't turn it back into the address" not in html
    assert "/v1/reports" in html and "400 days" in html
    assert reports.client_key("203.0.113.9") != hashlib.sha256(b"203.0.113.9").hexdigest()[:16]


def test_landing_says_estimate_official_first_and_links_accuracy():
    from rainout_agent.site_pages import landing_html
    html = landing_html().decode()
    assert "<strong>estimate</strong>" in html and "official status always comes first" in html
    assert html.count('href="/accuracy"') >= 3
    assert "37 were right and 2 were wrong" in html
