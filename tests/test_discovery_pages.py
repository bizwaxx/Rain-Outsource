import io
import json
import re
import struct
import zlib
from pathlib import Path

import pytest

from api.index import handler as IndexHandler
from rainout_agent import discovery_pages, metrics
from rainout_agent.http_app import application
from rainout_agent.mcp_server import SERVER_NAME, SERVER_VERSION, TOOLS

ROOT = Path(__file__).resolve().parents[1]
ADDRESS = "403 E Ben White Blvd, Ste B"


def _call(path, method="GET"):
    h = IndexHandler.__new__(IndexHandler)
    h.path = path
    h.wfile = io.BytesIO()
    h.headers = {}
    h.client_address = ("127.0.0.1", 0)
    out = {"headers": {}}
    h.send_response = lambda code, msg=None: out.__setitem__("status", code)
    h.send_header = lambda k, v: out["headers"].__setitem__(k, v)
    h.end_headers = lambda: None
    getattr(h, "do_" + method)()
    return out["status"], out["headers"], h.wfile.getvalue()


@pytest.mark.parametrize("path,ctype", [
    ("/privacy", "text/html"), ("/privacy/", "text/html"), ("/privacy.html", "text/html"),
    ("/terms", "text/html"), ("/terms.html", "text/html"),
    ("/icon.png", "image/png"),
    ("/.well-known/mcp/server-card.json", "application/json"), ("/.well-known/mcp/server-card", "application/json"),
])
def test_index_function_serves_discovery_paths(path, ctype):
    status, headers, body = _call(path)
    assert status == 200
    assert headers["Content-Type"].startswith(ctype)
    assert int(headers["Content-Length"]) == len(body) > 0
    assert "s-maxage" in headers["Cache-Control"]


def test_head_has_headers_but_no_body():
    status, headers, body = _call("/privacy", "HEAD")
    assert status == 200 and body == b"" and int(headers["Content-Length"]) > 0


def test_index_json_unchanged():
    status, headers, body = _call("/")
    assert status == 200 and headers["Content-Type"] == "application/json"
    assert json.loads(body)["service"] == "rainout-source"


@pytest.mark.parametrize("name", ["privacy.html", "terms.html"])
def test_legal_pages_are_marked_draft_and_carry_contact(name):
    html = (ROOT / "static_pages" / name).read_text()
    assert "DRAFT, not legal advice, Jimmy to review" in html
    assert ADDRESS in html and "Austin, TX 78704" in html
    assert '<meta name="robots" content="noindex">' in html
    assert "<script" not in html.lower()


def test_privacy_matches_what_the_code_does():
    html = (ROOT / "static_pages" / "privacy.html").read_text()
    # no accounts on the free tier
    assert "no accounts" in html
    # usage counter: daily-rotating keyed hash, HyperLogLog, no IP, 400 days
    assert "HMAC-SHA256" in html and "HyperLogLog" in html and "400 days" in html
    assert "raw IP address is never stored" in html
    # reports: 24 h, 280 chars, public, unsalted rate-limit hash described honestly
    assert "24 hours" in html and "280 characters" in html and "/v1/reports" in html
    assert "no secret key" in html
    # constants behind those claims
    from rainout_agent import reports
    assert reports.REPORT_TTL_S == 24 * 3600
    assert reports.MAX_NOTE_CHARS == 280
    assert metrics.TTL_S == 400 * 86400
    assert reports.client_key("203.0.113.9") != "203.0.113.9"
    day = metrics.today()
    assert metrics.client_hash("203.0.113.9", day) != metrics.client_hash("203.0.113.9", "2000-01-01")


def test_terms_say_estimates_and_official_status_wins_and_attribution():
    html = (ROOT / "static_pages" / "terms.html").read_text()
    assert "estimate" in html and "official status always wins" in html
    for credit in ("National Weather Service", "Open-Meteo", "CC BY 4.0", "OpenStreetMap", "ODbL", "NCEI"):
        assert credit in html
    from rainout_agent import osm_venues
    assert "OpenStreetMap contributors, ODbL 1.0" in osm_venues.ATTRIBUTION


def test_icon_is_a_valid_512_png_and_regenerates_identically():
    data = (ROOT / "static_pages" / "icon.png").read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    w, h = struct.unpack(">II", data[16:24])
    assert (w, h) == (512, 512)
    import importlib.util
    spec = importlib.util.spec_from_file_location("make_icon", ROOT / "scripts" / "make_icon.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.render() == data
    # chunks are well-formed
    pos = 8
    while pos < len(data):
        (n,) = struct.unpack(">I", data[pos:pos + 4])
        tag, body = data[pos + 4:pos + 8], data[pos + 8:pos + 8 + n]
        assert struct.unpack(">I", data[pos + 8 + n:pos + 12 + n])[0] == zlib.crc32(tag + body) & 0xFFFFFFFF
        pos += 12 + n


def test_server_card_matches_live_tools_and_version():
    status, _, body = _call("/.well-known/mcp/server-card.json")
    card = json.loads(body)
    assert card["serverInfo"] == {"name": SERVER_NAME, "title": "Rain-Outsource", "version": SERVER_VERSION}
    assert SERVER_VERSION == "0.13.0"
    assert [t["name"] for t in card["tools"]] == [t["name"] for t in TOOLS]
    assert len(card["tools"]) == 12
    assert card["transport"] == {"type": "streamable-http", "url": "https://rainout-agent-source.vercel.app/mcp"}
    assert card["authentication"] == {"required": False}
    for t in card["tools"]:
        assert t["description"] and t["inputSchema"]["type"] == "object"


def test_wsgi_app_serves_same_bytes():
    for path in ("/privacy", "/terms", "/icon.png", "/.well-known/mcp/server-card.json"):
        got = {}
        body = b"".join(application({"REQUEST_METHOD": "GET", "PATH_INFO": path, "wsgi.input": io.BytesIO()},
                                    lambda s, h: got.update(status=s, headers=dict(h))))
        assert got["status"].startswith("200")
        assert body == discovery_pages.lookup(path)[2]


def test_vercel_routes_point_at_existing_function_and_function_count_unchanged():
    cfg = json.loads((ROOT / "vercel.json").read_text())
    builds = [b["src"] for b in cfg["builds"]]
    assert len(builds) <= 12
    assert sorted(p.name for p in (ROOT / "api").glob("*.py") if p.name != "__init__.py") == sorted(Path(b).name for b in builds)
    for path in ("/privacy", "/privacy.html", "/terms", "/terms.html", "/icon.png",
                 "/.well-known/mcp/server-card.json", "/.well-known/mcp/server-card"):
        dest = next(r["dest"] for r in cfg["routes"] if re.fullmatch(r["src"], path))
        assert dest == "/api/index.py", path


def test_metrics_labels_are_bounded():
    assert metrics.endpoint_for("/privacy") == "privacy"
    assert metrics.endpoint_for("/terms.html") == "terms"
    assert metrics.endpoint_for("/icon.png") == "icon"
    assert metrics.endpoint_for("/.well-known/mcp/server-card.json") == "server_card"
    assert discovery_pages.lookup("/nope") is None
