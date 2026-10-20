"""Health endpoint for the LangGraph service."""

from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from scripts.config import get_settings

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("langgraph-service")

PORT = 8100


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path in ("/health", "/healthz", "/"):
            body = json.dumps({"status": "ok", "service": "langgraph"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args) -> None:
        return


def main() -> None:
    host = get_settings().bind_host
    log.info("langgraph-service listening on %s:%d", host, PORT)
    ThreadingHTTPServer((host, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
