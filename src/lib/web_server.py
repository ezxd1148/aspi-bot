"""Minimal HTTP server for Render health checks — no dependencies."""

import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *args):
        pass  # suppress request logs


def start() -> None:
    """Start a daemon HTTP server on $PORT (default 8080) in a background thread."""
    port = int(os.getenv("PORT", "8080"))

    def _run():
        server = HTTPServer(("0.0.0.0", port), _HealthHandler)
        print(f"Health server listening on port {port}")
        server.serve_forever()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
