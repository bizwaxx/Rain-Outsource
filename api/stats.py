from http.server import BaseHTTPRequestHandler
import json
from datetime import datetime, timezone

# Simple in-memory stats (resets on cold start)
# For production we would move this to Vercel KV or a database
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
        self.end_headers()
        self.wfile.write(json.dumps(body, indent=2).encode("utf-8"))
