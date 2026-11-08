#!/usr/bin/env python3
"""Tiny reverse proxy so the bridge dashboard can live in its own preview port.

The real bridge keeps port 8788 (that is the URL the relay talks to).
This serves the same dashboard + API on 8789 so it can be opened as a live
preview without competing for the bridge's port or its relay session.

    python3 preview.py            # listens on 0.0.0.0:8789 -> 127.0.0.1:8788
"""
import json
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = os.environ.get("BRIDGE_UPSTREAM", "http://127.0.0.1:8788")
PORT = int(os.environ.get("PREVIEW_PORT", "8789"))
ALLOWED = {"/", "/status", "/health", "/tools", "/logs", "/ping", "/execute",
           "/tools/call", "/rpc", "/cloud", "/api"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        pass

    def _proxy(self, method):
        path = self.path
        bare = path.split("?", 1)[0]
        if bare not in ALLOWED:
            body = json.dumps({"error": "not proxied", "path": bare}).encode()
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        data = None
        if method == "POST":
            n = int(self.headers.get("Content-Length") or 0)
            data = self.rfile.read(n) if n else b""

        req = urllib.request.Request(UPSTREAM + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                payload = resp.read()
                ctype = resp.headers.get("Content-Type", "application/json")
                code = resp.status
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            ctype = exc.headers.get("Content-Type", "application/json")
            code = exc.code
        except Exception as exc:                                     # noqa: BLE001
            payload = json.dumps({"error": f"bridge unreachable: {exc}"}).encode()
            ctype = "application/json"
            code = 502

        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        self._proxy("GET")

    def do_POST(self):
        self._proxy("POST")


if __name__ == "__main__":
    print(f"dashboard preview on http://0.0.0.0:{PORT} -> {UPSTREAM}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
