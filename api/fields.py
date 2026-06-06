import json
from http.server import BaseHTTPRequestHandler

from rainout_agent.status_api import list_supported_fields


def build_fields_payload():
    fields = list_supported_fields()
    return {
        "service": "rainout-source",
        "creator": "JEEZ Labs",
        "count": len(fields),
        "fields": fields,
    }


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self._send_json(200, build_fields_payload())

    def _send_json(self, status_code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
