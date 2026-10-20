#!/usr/bin/env python3
"""Smoke-test a Rainout Source deployment, marked as our own traffic so usage counters skip it.

Sends User-Agent rainout-smoke/1.0 and X-Rainout-Internal from $RAINOUT_INTERNAL_TOKEN or
~/.config/rainout/internal_token (the header is sent only to the deployment under test).

    python3 scripts/smoke_prod.py [https://rainout-agent-source.vercel.app]
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = (sys.argv[1] if len(sys.argv) > 1 else "https://rainout-agent-source.vercel.app").rstrip("/")


def internal_headers() -> dict:
    h = {"User-Agent": "rainout-smoke/1.0"}
    tok = os.environ.get("RAINOUT_INTERNAL_TOKEN")
    path = Path.home() / ".config/rainout/internal_token"
    if not tok and path.exists():
        tok = path.read_text().strip()
    if tok:
        h["X-Rainout-Internal"] = tok
    return h


def call(path, data=None):
    headers = internal_headers()
    if data is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(data).encode()
    req = urllib.request.Request(BASE + path, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, r.read()


GT = urllib.parse.quote((datetime.now(timezone(timedelta(hours=-5))) + timedelta(hours=8)).replace(minute=0, second=0, microsecond=0).isoformat())

CHECKS = [
    ("/health", None, lambda b: json.loads(b).get("ok") is not False),
    (f"/v1/status?field_id=Krieg&game_time={GT}", None, lambda b: "official_status" in json.loads(b)),
    ("/llms.txt", None, lambda b: len(b) > 100),
    ("/openapi.yaml", None, lambda b: b.startswith(b"openapi")),
    ("/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, lambda b: "tools" in json.loads(b)["result"]),
]

fails = 0
for path, data, ok in CHECKS:
    try:
        status, body = call(path, data)
        good = status == 200 and ok(body)
    except Exception as exc:  # noqa: BLE001
        good, status = False, exc.__class__.__name__
    fails += not good
    print(f"{'OK ' if good else 'FAIL'} {path} {status}")
sys.exit(1 if fails else 0)
