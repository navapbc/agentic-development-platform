"""Health and context-package endpoints for the Repository Intelligence Service."""

from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from scripts.config import get_settings

from services.repository_intelligence.context_builder.context_service import (
    RepositoryIntelligenceService,
)
from services.repository_intelligence.normalization.models import RepoRef
from services.repository_intelligence.normalization.serialization import context_package_to_dict

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("repository-intelligence")

HOST = "0.0.0.0"  # noqa: S104
PORT = 8120

_service: RepositoryIntelligenceService | None = None


def build_service() -> RepositoryIntelligenceService | None:
    """Return the warm RIS singleton, building it from settings on first use."""
    global _service
    if _service is not None:
        return _service
    settings = get_settings()
    root = settings.repo_intelligence_repo_root
    if not root:
        return None
    _service = RepositoryIntelligenceService.from_repo(
        root,
        commit=settings.repo_intelligence_commit or None,
        cbm_binary=settings.repo_intelligence_cbm_binary or None,
    )
    return _service


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path in ("/health", "/healthz", "/"):
            self._respond(200, {"status": "ok", "service": "repository-intelligence"})
        else:
            self._respond(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/context_package":
            self._respond(404, {"error": "not found"})
            return
        payload = self._read_json()
        if payload is None:
            self._respond(400, {"error": "invalid json"})
            return
        repo = payload.get("repo")
        focus = payload.get("focus")
        commit = payload.get("commit", "")
        if not repo or not focus:
            self._respond(400, {"error": "repo and focus are required"})
            return
        if repo not in get_settings().allowed_repos():
            self._respond(403, {"error": "repo not allowlisted"})
            return
        service = build_service()
        if service is None:
            self._respond(503, {"error": "repository intelligence not configured"})
            return
        package = service.context_package(RepoRef(repo, commit), focus)
        self._respond(200, context_package_to_dict(package))

    def _read_json(self) -> dict[str, Any] | None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return None

    def _respond(self, code: int, body: dict[str, Any]) -> None:
        encoded = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *args) -> None:
        return


def main() -> None:
    log.info("repository-intelligence listening on %s:%d", HOST, PORT)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
