"""Static discovery pages: landing page, icons, robots/sitemap, privacy/terms and the MCP server card.

Shared by the Vercel handlers (``api/site.py`` and ``api/index.py``) and the local WSGI app, so
both serve identical bytes. Files live in ``site/`` at the repo root.

The landing page sits on ``/`` next to the JSON service index. JSON stays the default: only a client
that explicitly asks for ``text/html`` (a browser or a search crawler) and doesn't prefer JSON gets
HTML. ``?format=json`` / ``?format=html`` force one or the other.
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

SITE_DIR = Path(__file__).resolve().parents[1] / "site"

# Browsers revalidate after 5 min; the Vercel edge keeps static assets for a day.
STATIC_CACHE = "public, max-age=300, s-maxage=86400"
ICON_CACHE = "public, max-age=86400, s-maxage=604800"
# "/" varies on Accept, so keep it out of the shared edge cache (browser cache only).
ROOT_CACHE = "public, max-age=300"

SERVER_CARD_PATH = "/.well-known/mcp/server-card.json"

# path -> (file in site/, content type, cache-control)
STATIC_FILES: dict[str, tuple[str, str, str]] = {
    "/icon.svg": ("icon.svg", "image/svg+xml", ICON_CACHE),
    "/favicon.svg": ("icon.svg", "image/svg+xml", ICON_CACHE),
    "/icon.png": ("icon.png", "image/png", ICON_CACHE),
    "/icon-400.png": ("icon-400.png", "image/png", ICON_CACHE),
    "/apple-touch-icon.png": ("icon.png", "image/png", ICON_CACHE),
    "/favicon.ico": ("favicon.ico", "image/x-icon", ICON_CACHE),
    "/robots.txt": ("robots.txt", "text/plain; charset=utf-8", STATIC_CACHE),
    "/sitemap.xml": ("sitemap.xml", "application/xml; charset=utf-8", STATIC_CACHE),
    "/privacy": ("privacy.html", "text/html; charset=utf-8", STATIC_CACHE),
    "/terms": ("terms.html", "text/html; charset=utf-8", STATIC_CACHE),
    "/accuracy": ("accuracy.html", "text/html; charset=utf-8", STATIC_CACHE),
    "/switch": ("switch.html", "text/html; charset=utf-8", STATIC_CACHE),
    "/switch/embed": ("embed.html", "text/html; charset=utf-8", STATIC_CACHE),
    "/switch.js": ("switch.js", "application/javascript; charset=utf-8", STATIC_CACHE),
    "/switch-embed.js": ("switch-embed.js", "application/javascript; charset=utf-8", STATIC_CACHE),
}
ALIASES = {"/privacy.html": "/privacy", "/terms.html": "/terms", "/accuracy.html": "/accuracy", "/switch.html": "/switch"}


def _parse_accept(accept: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for part in (accept or "").split(","):
        bits = [b.strip() for b in part.split(";")]
        media = bits[0].lower()
        if not media:
            continue
        q = 1.0
        for b in bits[1:]:
            if b.startswith("q="):
                try:
                    q = float(b[2:])
                except ValueError:
                    q = 0.0
        out[media] = max(q, out.get(media, 0.0))
    return out


def wants_html(accept: str | None, query: str = "") -> bool:
    """True only when the client explicitly prefers HTML over JSON. ``*/*`` and missing Accept get JSON."""
    fmt = (parse_qs(query or "").get("format") or [""])[0].lower()
    if fmt in {"json", "html"}:
        return fmt == "html"
    prefs = _parse_accept(accept or "")
    html_q = max(prefs.get("text/html", 0.0), prefs.get("application/xhtml+xml", 0.0))
    json_q = prefs.get("application/json", 0.0)
    return html_q > 0 and html_q > json_q


def landing_html() -> bytes:
    return (SITE_DIR / "index.html").read_bytes()


def server_card() -> dict:
    """MCP server card generated from the live tool list, so it never drifts from tools/list."""
    from rainout_agent.mcp_server import SERVER_NAME, SERVER_VERSION, TOOLS

    return {
        "serverInfo": {"name": SERVER_NAME, "title": "Rain-Outsource", "version": SERVER_VERSION},
        "authentication": {"required": False},
        "tools": TOOLS,
        "resources": [],
        "prompts": [],
    }


def lookup(path: str) -> tuple[int, str, bytes, str] | None:
    """Return (status, content_type, body, cache_control) for a static discovery path, else None."""
    path = urlsplit(path).path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    path = ALIASES.get(path, path)
    if path == SERVER_CARD_PATH or path == "/.well-known/mcp/server-card":
        body = json.dumps(server_card(), indent=2).encode("utf-8")
        return 200, "application/json", body, STATIC_CACHE
    if path in STATIC_FILES:
        name, ctype, cache = STATIC_FILES[path]
        return 200, ctype, (SITE_DIR / name).read_bytes(), cache
    # Repo-root docs served by this function too (keeps the deployment under Vercel Hobby's 12-function limit).
    if path == "/llms.txt":
        return 200, "text/plain; charset=utf-8", (SITE_DIR.parent / "llms.txt").read_bytes(), "public, max-age=3600, s-maxage=86400"
    if path == "/openapi.yaml":
        from rainout_agent import http_cache

        return 200, "application/yaml; charset=utf-8", (SITE_DIR.parent / "openapi.yaml").read_bytes(), http_cache.OPENAPI
    return None
