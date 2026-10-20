from http.server import BaseHTTPRequestHandler
import json
from datetime import datetime, timezone
from urllib.parse import urlparse

from rainout_agent import metrics
from rainout_agent.admin_api import handle_admin_usage

# Simple in-memory stats (resets on cold start). Persistent daily counters live in
# rainout_agent.metrics and are read at /v1/admin/usage (admin token).
STATS = {
    "total_calls": 0,
    "calls_today": 0,
    "last_reset_date": None
}


def _reset_if_new_day():
    today = datetime.now(timezone.utc).date().isoformat()
    if STATS["last_reset_date"] != today:
        STATS["calls_today"] = 0
        STATS["last_reset_date"] = today


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path.rstrip("/") == "/v1/admin/usage":
            status, ctype, payload = handle_admin_usage(parsed.query, dict(self.headers.items()))
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        _reset_if_new_day()
        STATS["total_calls"] += 1
        STATS["calls_today"] += 1

        body = {
            "service": "rainout-source",
            "today": STATS["last_reset_date"],
            "calls_today": STATS["calls_today"],
            "total_calls": STATS["total_calls"],
            "note": "Stats are in-memory for the pilot. Upgrade to persistent storage coming next."
        }

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(json.dumps(body, indent=2).encode("utf-8"))


metrics.instrument(handler)
