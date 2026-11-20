"""Smoke tests for the repository-intelligence server entrypoint."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest
from services.http_front_door import MAX_REQUEST_BODY_BYTES
from services.integration.tenant_config import DirectorySource
from services.repository_intelligence import server
from services.repository_intelligence.context_builder.registry import (
    RepositoryIntelligenceRegistry,
)
from services.repository_intelligence.indexing.reindex import IndexRoots
from services.repository_intelligence.normalization.models import ContextPackage, RepoRef
from services.repository_intelligence.tenant_tokens import (
    tenant_for_token as server_tenant_for_token,
)
from services.repository_intelligence.tenants import TenantRegistries
from services.repository_intelligence.token_secrets import StaticTokens
from tests.support import hermetic_settings


class _Recorder(server.Handler):
    def __init__(self, path, body=b""):
        self.path = path
        self.headers = {"Content-Length": str(len(body))}
        self.rfile = io.BytesIO(body)
        self._status = None
        self._headers: dict[str, str] = {}
        self._body = b""
        self.wfile = self

    def send_response(self, code):
        self._status = code

    def send_header(self, key, value):
        self._headers[key] = value

    def end_headers(self):
        return

    def write(self, data):
        self._body = data


def test_server_exposes_entrypoint():
    assert callable(server.main)
    assert isinstance(server.PORT, int)


def test_handler_reports_health():
    class _Recorder(server.Handler):
        def __init__(self):
            self.path = "/health"
            self._status = None
            self._headers: dict[str, str] = {}
            self._body = b""
            self.wfile = self

        def send_response(self, code):
            self._status = code

        def send_header(self, key, value):
            self._headers[key] = value

        def end_headers(self):
            return

        def write(self, data):
            self._body = data

    recorder = _Recorder()
    recorder.do_GET()

    assert recorder._status == 200
    assert b"repository-intelligence" in recorder._body


ADP_TOKEN = "token-adp"
BETA_TOKEN = "token-beta"


class _FakeService:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.warmed: list[RepoRef] = []

    def context_package(self, repo, focus, detail=""):
        return ContextPackage(
            focus=focus,
            symbols=(),
            references=(),
            related_tests=(),
            dependencies=(),
            repository=repo.repo,
            indexed_commit=str(self.root),
        )

    def ensure_fresh(self, repo):
        self.warmed.append(repo)
        return True


def _bundle(root: Path, tenant: str, repositories: str) -> None:
    directory = root / "tenants" / tenant
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "repositories.yaml").write_text(
        "config_version: 1\n" + repositories, encoding="utf-8"
    )


@pytest.fixture
def served(tmp_path, monkeypatch):
    """Serve adp and beta, each naming an indexed web and beta also an unindexed beta-docs."""
    _bundle(
        tmp_path, "adp", "repositories:\n  web:\n    index: true\n  adp-api:\n    index: true\n"
    )
    _bundle(
        tmp_path,
        "beta",
        "repositories:\n  web:\n    index: true\n  beta-docs:\n    index: false\n",
    )
    settings = hermetic_settings(tenants="adp,beta", tenant="adp")
    registries = TenantRegistries(
        settings,
        source_factory=lambda name: DirectorySource(tmp_path / "tenants" / name, name),
    )
    built: list[tuple[Path, object]] = []
    services: dict[Path, _FakeService] = {}

    def builder(root, *, commit=None, cbm_binary=None, index_roots=None) -> Any:
        built.append((Path(root), index_roots))
        return services.setdefault(Path(root), _FakeService(Path(root)))

    monkeypatch.setattr(server, "get_settings", lambda: settings)
    monkeypatch.setattr(server, "tenant_registries", lambda: registries)
    tokens = StaticTokens({"adp": (ADP_TOKEN,), "beta": (BETA_TOKEN,)})
    monkeypatch.setattr(server, "token_source", lambda: tokens)
    monkeypatch.setattr(server, "_registry", RepositoryIntelligenceRegistry([], builder=builder))
    return built


def _post(path: str, payload: dict, token: str | None = None) -> _Recorder:
    recorder = _Recorder(path, json.dumps(payload).encode())
    if token is not None:
        recorder.headers["Authorization"] = f"Bearer {token}"
    recorder.do_POST()
    return recorder


def test_context_package_endpoint_returns_serialized_package(served):
    recorder = _post("/context_package", {"repo": "adp-api", "focus": "health"}, ADP_TOKEN)

    assert recorder._status == 200
    payload = json.loads(recorder._body)
    assert payload["repository"] == "adp-api"
    assert payload["focus"] == "health"


def test_two_tenants_with_the_same_repository_receive_their_own_packages(served):
    adp = _post("/context_package", {"repo": "web", "focus": "health"}, ADP_TOKEN)
    beta = _post("/context_package", {"repo": "web", "focus": "health"}, BETA_TOKEN)

    assert adp._status == beta._status == 200
    assert json.loads(adp._body)["indexed_commit"] == "/tenants/adp/repositories/web"
    assert json.loads(beta._body)["indexed_commit"] == "/tenants/beta/repositories/web"
    roots = {root: index_roots for root, index_roots in served}
    adp_roots = roots[Path("/tenants/adp/repositories/web")]
    beta_roots = roots[Path("/tenants/beta/repositories/web")]
    assert isinstance(adp_roots, IndexRoots) and isinstance(beta_roots, IndexRoots)
    assert adp_roots.scip.name == "adp" and beta_roots.scip.name == "beta"
    assert adp_roots.zoekt.name == "adp" and beta_roots.zoekt.name == "beta"


@pytest.mark.parametrize("path", ["/context_package", "/warm"])
def test_a_request_without_a_token_is_refused_with_401(served, path):
    recorder = _post(path, {"repo": "web", "focus": "health"})

    assert recorder._status == 401
    assert "error" in json.loads(recorder._body)
    assert recorder.close_connection is True


@pytest.mark.parametrize("path", ["/context_package", "/warm"])
def test_a_request_with_an_unknown_token_is_refused_with_401(served, path):
    recorder = _post(path, {"repo": "web", "focus": "health"}, "not-a-token")

    assert recorder._status == 401


def test_a_request_with_a_non_bearer_scheme_is_refused_with_401(served):
    recorder = _Recorder("/context_package", b"{}")
    recorder.headers["Authorization"] = f"Basic {ADP_TOKEN}"
    recorder.do_POST()

    assert recorder._status == 401


@pytest.mark.parametrize("path", ["/context_package", "/warm"])
def test_another_tenants_repository_is_refused_with_403(served, path):
    recorder = _post(path, {"repo": "beta-docs", "focus": "health"}, ADP_TOKEN)

    assert recorder._status == 403
    assert json.loads(recorder._body) == {"error": "repo not allowlisted"}


def test_the_server_resolves_the_token_through_tenant_for_token(served, monkeypatch):
    seen: list[tuple[dict, str]] = []

    def recording(tokens, presented):
        seen.append((dict(tokens), presented))
        return server_tenant_for_token(tokens, presented)

    monkeypatch.setattr(server, "tenant_for_token", recording)

    recorder = _post("/context_package", {"repo": "web", "focus": "health"}, BETA_TOKEN)

    assert recorder._status == 200
    assert seen == [({"adp": (ADP_TOKEN,), "beta": (BETA_TOKEN,)}, BETA_TOKEN)]


class _RotatingTokens:
    """A token source whose read after a miss returns the tokens of a completed rotation."""

    def __init__(self, before: dict, after: dict | None) -> None:
        self.before = before
        self.after = after
        self.misses = 0

    def accepted(self):
        return self.before

    def reload_after_miss(self):
        self.misses += 1
        return self.after


def test_the_previous_and_the_current_token_of_a_tenant_are_both_accepted(served, monkeypatch):
    tokens = StaticTokens({"adp": ("adp-new", ADP_TOKEN), "beta": (BETA_TOKEN,)})
    monkeypatch.setattr(server, "token_source", lambda: tokens)

    for presented in ("adp-new", ADP_TOKEN):
        recorder = _post("/context_package", {"repo": "adp-api", "focus": "health"}, presented)
        assert recorder._status == 200


def test_an_unknown_token_reads_the_tokens_again_and_accepts_a_newly_rotated_one(
    served, monkeypatch
):
    source = _RotatingTokens(
        {"adp": (ADP_TOKEN,)}, {"adp": ("adp-rotated", ADP_TOKEN), "beta": (BETA_TOKEN,)}
    )
    monkeypatch.setattr(server, "token_source", lambda: source)

    recorder = _post("/context_package", {"repo": "adp-api", "focus": "health"}, "adp-rotated")

    assert recorder._status == 200
    assert source.misses == 1


def test_a_known_token_does_not_read_the_tokens_again(served, monkeypatch):
    source = _RotatingTokens({"adp": (ADP_TOKEN,)}, None)
    monkeypatch.setattr(server, "token_source", lambda: source)

    recorder = _post("/context_package", {"repo": "adp-api", "focus": "health"}, ADP_TOKEN)

    assert recorder._status == 200
    assert source.misses == 0


def test_an_unknown_token_is_refused_when_no_new_read_is_due(served, monkeypatch):
    source = _RotatingTokens({"adp": (ADP_TOKEN,)}, None)
    monkeypatch.setattr(server, "token_source", lambda: source)

    recorder = _post("/context_package", {"repo": "adp-api", "focus": "health"}, "adp-rotated")

    assert recorder._status == 401
    assert source.misses == 1


def test_no_configured_token_refuses_every_request(served, monkeypatch):
    monkeypatch.setattr(server, "token_source", lambda: StaticTokens({}))

    recorder = _post("/context_package", {"repo": "web", "focus": "health"}, ADP_TOKEN)

    assert recorder._status == 401


def test_a_repository_configured_with_index_false_is_unindexed(served):
    recorder = _post("/context_package", {"repo": "beta-docs", "focus": "health"}, BETA_TOKEN)

    assert recorder._status == 200
    package = json.loads(recorder._body)
    assert package["repository"] == "beta-docs"
    assert package["indexed_commit"] is None
    assert package["symbols"] == []


def test_an_indexed_repository_missing_from_the_warm_registry_returns_503(served, monkeypatch):
    class _Empty:
        def service(self, repo, tenant=""):
            return None

    monkeypatch.setattr(server, "build_registry", lambda: _Empty())

    recorder = _post("/context_package", {"repo": "web", "focus": "health"}, ADP_TOKEN)

    assert recorder._status == 503


def test_warm_reindexes_the_tenants_own_checkout(served):
    recorder = _post("/warm", {"repo": "web", "commit": "abc1234"}, BETA_TOKEN)

    assert recorder._status == 200
    assert json.loads(recorder._body) == {"repo": "web", "commit": "abc1234", "reindexed": True}
    assert [root for root, _ in served] == [Path("/tenants/beta/repositories/web")]


def test_a_missing_focus_is_rejected_after_authentication(served):
    assert _post("/context_package", {"repo": "web"}, ADP_TOKEN)._status == 400


def test_an_unknown_path_is_not_found_without_authentication(served):
    assert _post("/other", {})._status == 404


def test_build_registry_keys_the_indexed_repositories_by_tenant(served):
    registry = server.build_registry()

    assert registry.repos() == {("adp", "web"), ("adp", "adp-api"), ("beta", "web")}


def test_over_cap_content_length_returns_413_leaving_the_body_unread(served):
    recorder = _Recorder("/context_package")
    recorder.headers["Content-Length"] = str(MAX_REQUEST_BODY_BYTES + 1)
    recorder.headers["Authorization"] = f"Bearer {ADP_TOKEN}"
    recorder.rfile = io.BytesIO(b"x" * 16)

    recorder.do_POST()

    assert recorder._status == 413
    assert recorder.rfile.tell() == 0


def test_non_numeric_content_length_returns_400(served):
    recorder = _Recorder("/context_package")
    recorder.headers["Content-Length"] = "banana"
    recorder.headers["Authorization"] = f"Bearer {ADP_TOKEN}"

    recorder.do_POST()

    assert recorder._status == 400
