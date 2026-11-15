"""Health and context-package endpoints for the Repository Intelligence Service."""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, BinaryIO, cast

from scripts.config import configured_log_level, get_settings

from services.http_front_door import (
    REQUEST_TIMEOUT_SECONDS,
    RequestRejected,
    read_bounded_body,
)
from services.integration.repositories import Repository
from services.repository_intelligence.context_builder.registry import (
    RepositoryIntelligenceRegistry,
    RepoSpec,
)
from services.repository_intelligence.normalization.models import ContextPackage, RepoRef
from services.repository_intelligence.normalization.serialization import context_package_to_dict
from services.repository_intelligence.tenant_tokens import tenant_for_token
from services.repository_intelligence.tenants import TenantRegistries, migrate_home_indexes
from services.repository_intelligence.token_secrets import ARN_MAP_ENV, TokenSource
from services.repository_intelligence.token_secrets import token_source as build_token_source

logging.basicConfig(level=configured_log_level())
log = logging.getLogger("repository-intelligence")

PORT = 8120

_state_lock = threading.Lock()
_tenant_registries: TenantRegistries | None = None
_registry: RepositoryIntelligenceRegistry | None = None
_token_source: TokenSource | None = None


def tenant_registries() -> TenantRegistries:
    """Return the tenant registries singleton, building it from settings on first use."""
    global _tenant_registries
    with _state_lock:
        if _tenant_registries is None:
            _tenant_registries = TenantRegistries(get_settings())
        return _tenant_registries


def token_source() -> TokenSource:
    """Return the token source singleton, built from settings and the environment on first use."""
    global _token_source
    with _state_lock:
        if _token_source is None:
            _token_source = build_token_source(get_settings())
        return _token_source


def build_registry() -> RepositoryIntelligenceRegistry:
    """Return the warm registry singleton, synced to the indexed repositories of every tenant."""
    global _registry
    settings = get_settings()
    cbm_binary = settings.repo_intelligence_cbm_binary or None
    specs = [
        RepoSpec(
            name=entry.name,
            root=entry.root,
            commit=entry.ref,
            cbm_binary=cbm_binary,
            tenant=entry.tenant,
            index_roots=entry.index_roots,
        )
        for entry in tenant_registries().indexed()
    ]
    with _state_lock:
        if _registry is None:
            _registry = RepositoryIntelligenceRegistry(specs)
            return _registry
    _registry.replace_specs(specs)
    return _registry


def unindexed_context_package(repo: str, focus: str) -> ContextPackage:
    """The package for a repository configured with index false, which carries no code context."""
    return ContextPackage(
        focus=focus,
        symbols=(),
        references=(),
        related_tests=(),
        dependencies=(),
        repository=repo,
    )


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
        if self.path not in ("/context_package", "/warm"):
            self._respond(404, {"error": "not found"})
            return
        tenant = self._authenticated_tenant()
        if tenant is None:
            self.close_connection = True
            self._respond(401, {"error": "a known bearer token is required"})
            return
        if self.path == "/warm":
            self._warm(tenant)
        else:
            self._context_package(tenant)

    def _authenticated_tenant(self) -> str | None:
        scheme, _, presented = (self.headers.get("Authorization") or "").partition(" ")
        presented = presented.strip()
        if scheme.lower() != "bearer" or not presented:
            return None
        source = token_source()
        tenant = tenant_for_token(source.accepted(), presented)
        if tenant is None:
            reloaded = source.reload_after_miss()
            if reloaded is not None:
                tenant = tenant_for_token(reloaded, presented)
        return tenant

    def _repository(self, tenant: str, repo: str) -> Repository | None:
        registry = tenant_registries().registry(tenant)
        return registry.get(repo) if registry is not None else None

    def _context_package(self, tenant: str) -> None:
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
        repository = self._repository(tenant, repo)
        if repository is None:
            self._respond(403, {"error": "repo not allowlisted"})
            return
        if not repository.index:
            self._respond(200, context_package_to_dict(unindexed_context_package(repo, focus)))
            return
        service = build_registry().service(repo, tenant)
        if service is None:
            self._respond(503, {"error": f"repository intelligence has no index for {repo}"})
            return
        package = service.context_package(RepoRef(repo, commit), focus, detail)
        self._respond(200, context_package_to_dict(package))

    def _warm(self, tenant: str) -> None:
        payload = self._read_json()
        if payload is None:
            self._respond(400, {"error": "invalid json"})
            return
        repo = payload.get("repo")
        commit = payload.get("commit", "")
        if not repo:
            self._respond(400, {"error": "repo is required"})
            return
        if self._repository(tenant, repo) is None:
            self._respond(403, {"error": "repo not allowlisted"})
            return
        service = build_registry().service(repo, tenant)
        if service is None:
            self._respond(503, {"error": "repository intelligence not configured"})
            return
        reindexed = service.ensure_fresh(RepoRef(repo, commit))
        self._respond(200, {"repo": repo, "commit": commit, "reindexed": reindexed})

    def _read_json(self) -> dict[str, Any] | None:
        raw = read_bounded_body(cast(BinaryIO, self.rfile), cast(Mapping[str, str], self.headers))
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
    from services.server_tls import wrap

    settings = get_settings()
    if not token_source().accepted():
        log.error(
            "no tenant token is configured, so every context and warm request is refused. "
            f"Set {ARN_MAP_ENV} in AWS, REPO_INTELLIGENCE_TOKEN_<TENANT> for each tenant or "
            "REPO_INTELLIGENCE_TOKEN_KEY on a workstation, or REPO_INTELLIGENCE_TOKEN when "
            "neither TENANTS nor TENANT is set."
        )
    migrate_home_indexes(tenant_registries(), settings)
    server = ThreadingHTTPServer((settings.bind_host, PORT), Handler)
    wrap(server, enabled=settings.server_tls, configured_dir=settings.server_tls_dir)
    scheme = "https" if settings.server_tls else "http"
    log.info("repository-intelligence listening on %s://%s:%d", scheme, settings.bind_host, PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
