"""HTTP entrypoint for the PR-closed webhook receiver."""

from __future__ import annotations

import asyncio
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from scripts.config import get_settings

from services.orchestration.webhook.pr_events import (
    PostgresWorkflowResolver,
    TemporalSignaller,
    WorkflowResolver,
    WorkflowSignaller,
    handle_pr_webhook,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pr-webhook")

HOST = "0.0.0.0"  # noqa: S104
PORT = 8140
_SIGNATURE_HEADER = "X-Hub-Signature-256"


def build_handler(
    resolver: WorkflowResolver, signaller: WorkflowSignaller
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path in ("/health", "/healthz", "/"):
                self._respond(200, {"status": "ok", "service": "pr-webhook"})
            else:
                self._respond(404, {"error": "not found"})

        def do_POST(self) -> None:
            if self.path != "/webhook/pr":
                self._respond(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            settings = get_settings()
            code, payload = asyncio.run(
                handle_pr_webhook(
                    body,
                    self.headers.get(_SIGNATURE_HEADER, ""),
                    secret=settings.github_webhook_secret,
                    allowed_repos=frozenset(settings.allowed_repos()),
                    resolver=resolver,
                    signaller=signaller,
                )
            )
            self._respond(code, payload)

        def _respond(self, code: int, body: dict[str, Any]) -> None:
            import json

            encoded = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main() -> None:
    resolver = PostgresWorkflowResolver()
    signaller = asyncio.run(TemporalSignaller.connect())
    handler = build_handler(resolver, signaller)
    log.info("pr-webhook listening on %s:%d", HOST, PORT)
    ThreadingHTTPServer((HOST, PORT), handler).serve_forever()


if __name__ == "__main__":
    main()
