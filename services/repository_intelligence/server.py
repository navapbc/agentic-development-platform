"""Health and context-package endpoints for the Repository Intelligence Service."""

from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from scripts.config import configured_log_level, get_settings

from services.http_front_door import (
    REQUEST_TIMEOUT_SECONDS,
    RequestRejected,
    read_bounded_body,
)
from services.integration.source_control import source_control_repos
from services.repository_intelligence.context_builder.registry import (
    RepositoryIntelligenceRegistry,
    RepoSpec,
)
from services.repository_intelligence.normalization.models import RepoRef
from services.repository_intelligence.normalization.serialization import context_package_to_dict

logging.basicConfig(level=configured_log_level())
log = logging.getLogger("repository-intelligence")

PORT = 8120

_registry: RepositoryIntelligenceRegistry | None = None


def build_registry() -> RepositoryIntelligenceRegistry | None:
    """Return the warm registry singleton, building it from settings on first use."""
    global _registry
    if _registry is not None:
        return _registry
    settings = get_settings()
    configured = settings.repo_intelligence_registry()
    if not configured:
        return None
    cbm_binary = settings.repo_intelligence_cbm_binary or None
    specs = [
        RepoSpec(name=name, root=Path(path), commit=commit, cbm_binary=cbm_binary)
        for name, (path, commit) in configured.items()
    ]
    _registry = RepositoryIntelligenceRegistry(specs)
    return _registry


class Handler(BaseHTTPRequestHandler):
    timeout = REQUEST_TIMEOUT_SECONDS

    def do_GET(self) -> None:
        if self.path in ("/health", "/healthz", "/"):
            self._respond(200, {"status": "ok", "service": "repository-intelligence"})
        else:
            self._respond(404, {"error": "not found"})

    def do_POST(self) -> None:
        try:
            self._route()
        except RequestRejected as exc:
            self.close_connection = True
            self._respond(exc.status, {"error": exc.message})

    def _route(self) -> None:
        if self.path == "/warm":
            self._warm()
            return
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
        detail = payload.get("detail", "")
        if not repo or not focus:
            self._respond(400, {"error": "repo and focus are required"})
            return
        if repo not in source_control_repos(get_settings()):
            self._respond(403, {"error": "repo not allowlisted"})
            return
        registry = build_registry()
        service = registry.service(repo) if registry is not None else None
        if service is None:
            self._respond(503, {"error": "repository intelligence not configured"})
            return
        package = service.context_package(RepoRef(repo, commit), focus, detail)
        self._respond(200, context_package_to_dict(package))

    def _warm(self) -> None:
        payload = self._read_json()
        if payload is None:
            self._respond(400, {"error": "invalid json"})
            return
        repo = payload.get("repo")
        commit = payload.get("commit", "")
        if not repo:
            self._respond(400, {"error": "repo is required"})
            return
        if repo not in source_control_repos(get_settings()):
            self._respond(403, {"error": "repo not allowlisted"})
            return
        registry = build_registry()
        service = registry.service(repo) if registry is not None else None
        if service is None:
            self._respond(503, {"error": "repository intelligence not configured"})
            return
        reindexed = service.ensure_fresh(RepoRef(repo, commit))
        self._respond(200, {"repo": repo, "commit": commit, "reindexed": reindexed})

    def _read_json(self) -> dict[str, Any] | None:
        raw = read_bounded_body(self.rfile, self.headers)
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
    host = get_settings().bind_host
    log.info("repository-intelligence listening on %s:%d", host, PORT)
    ThreadingHTTPServer((host, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
