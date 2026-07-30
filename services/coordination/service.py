"""Coordination-service entrypoint: health endpoint backed by an agent_platform ping."""

from __future__ import annotations

import asyncio
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from scripts.config import get_settings
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("coordination-service")

HOST = "0.0.0.0"  # noqa: S104
PORT = 8130


async def _ping(database_url: str) -> None:
    engine = create_async_engine(database_url, future=True)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    finally:
        await engine.dispose()


def _agent_platform_healthy() -> bool:
    try:
        asyncio.run(_ping(get_settings().database_url))
        return True
    except Exception:
        log.exception("agent_platform ping failed")
        return False


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path in ("/health", "/healthz", "/"):
            healthy = _agent_platform_healthy()
            body = json.dumps(
                {"status": "ok" if healthy else "degraded", "service": "coordination"}
            ).encode()
            self.send_response(200 if healthy else 503)
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
    log.info("coordination-service listening on %s:%d", HOST, PORT)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
