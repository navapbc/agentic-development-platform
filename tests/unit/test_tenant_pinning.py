"""Every activity of a run reads the config bundle the run's manifest pinned."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from scripts.config import get_settings
from services.integration import tenant_config
from services.integration.tenant_config import DirectorySource, current_identity, tenant_bundle_dir
from services.orchestration.tenant_pinning import TENANT_MISMATCH, TenantConfigInterceptor
from temporalio.exceptions import ApplicationError
from tests.support import write_image_policy

REPOSITORIES = """config_version: 1
defaults:
  backend: local
repositories:
  {name}:
    path: ../{name}
"""
TICKETS = """config_version: 1
backend:
  repo_root: /srv/rebar-tickets
  project: ADSER
  default_repo: svc
  status_flow:
    work_started: in_progress
    ready_for_review: in_progress
    terminal: closed
"""
GATES = """config_version: 1
profile: private
gates:
  - name: semgrep
    enabled: false
"""


class _Next:
    def __init__(self) -> None:
        self.seen = []

    async def execute_activity(self, input):
        self.seen.append((tenant_bundle_dir(), current_identity()))
        return "done"


@pytest.fixture
def config(tmp_path, monkeypatch):
    env = tmp_path / "env"
    env.mkdir()
    (env / "gates.yaml").write_text(GATES)
    write_image_policy(env)
    monkeypatch.setenv("PLATFORM_ENV_CONFIG_DIR", str(env))
    source = tmp_path / "source"
    source.mkdir()
    (source / "tickets.yaml").write_text(TICKETS)
    (source / "repositories.yaml").write_text(REPOSITORIES.format(name="svc"))
    settings = get_settings().model_copy(update={"tenant": "adp", "ticket_backend": "rebar"})
    tenant_config.reset()
    configured = tenant_config.configure(
        settings, source=DirectorySource(source, "adp"), store_root=tmp_path / "store"
    )
    yield configured, source
    tenant_config.reset()


def _input(execution_id: str = "exec-1"):
    return SimpleNamespace(args=[SimpleNamespace(execution_id=execution_id)])


async def test_a_run_reads_its_pinned_bundle_after_a_newer_one_activates(config) -> None:
    configured, source = config
    recorded = ("adp", configured.active.to_manifest())
    lookups = []

    async def lookup(execution_id):
        lookups.append(execution_id)
        return recorded

    next_ = _Next()
    inbound = TenantConfigInterceptor(configured, lookup).intercept_activity(next_)
    (source / "repositories.yaml").write_text(REPOSITORIES.format(name="web"))
    assert configured.refresh() is True

    await inbound.execute_activity(_input())
    await inbound.execute_activity(_input())

    path, identity = next_.seen[0]
    assert "svc" in (path / "repositories.yaml").read_text()
    assert identity.id == recorded[1]["id"]
    assert lookups == ["exec-1"]


async def test_an_activity_before_the_manifest_reads_the_active_bundle(config) -> None:
    configured, _ = config

    async def lookup(execution_id):
        return None

    next_ = _Next()
    inbound = TenantConfigInterceptor(configured, lookup).intercept_activity(next_)

    await inbound.execute_activity(_input())

    assert next_.seen == [(configured.active_path, configured.active)]


async def test_a_run_of_another_tenant_fails_without_retry(config) -> None:
    configured, _ = config

    async def lookup(execution_id):
        return ("payments", {"source": "directory", "files": {}})

    inbound = TenantConfigInterceptor(configured, lookup).intercept_activity(_Next())

    with pytest.raises(ApplicationError) as raised:
        await inbound.execute_activity(_input())

    assert raised.value.type == TENANT_MISMATCH
    assert raised.value.non_retryable


DIGEST = "sha256:" + "a" * 64
ENGINE = """config_version: 1
engines:
  - name: {name}
    enabled: true
    mode: image
    image: registry.example/{name}@{digest}
    review_entrypoint: bin/review
"""
PLATFORM_REGISTRIES = """config_version: 1
mode: external
mirrors:
  docker.io: artifactory.example.gov/docker-remote
"""
TENANT_REGISTRIES = """config_version: 1
credential:
  username: {username}
"""


class _Probe:
    def __init__(self, read) -> None:
        self.read = read
        self.seen = []

    async def execute_activity(self, input):
        self.seen.append(self.read())
        return "done"


def _publish(configured, source, **files: str):
    for name, text in files.items():
        (source / f"{name}.yaml").write_text(text)
    assert configured.refresh() is True
    return configured.active


def _review_inbound(monkeypatch, configured, next_, *, recorded_tenant: str, bundle):
    from functools import partial

    from services.orchestration import worker
    from services.orchestration.tenant_attribution import TenantAttributionInterceptor

    async def recorded_bundle(execution_id):
        return bundle

    async def execution_tenant(execution_id):
        return recorded_tenant

    monkeypatch.setattr(
        "services.orchestration.tenant_pinning.TenantConfigInterceptor",
        partial(TenantConfigInterceptor, lookup=recorded_bundle),
    )
    monkeypatch.setattr(
        "services.orchestration.tenant_attribution.recorded_execution_tenant", execution_tenant
    )
    monkeypatch.setattr(worker, "get_settings", lambda: configured.settings)
    built = {}
    monkeypatch.setattr(worker, "Worker", lambda *args, **kwargs: built.update(kwargs))
    worker.build_worker(None, "agent-platform-review-adp", worker.WORKER_ROLE_REVIEW)
    tenant_interceptors = [
        interceptor
        for interceptor in built["interceptors"]
        if isinstance(interceptor, TenantAttributionInterceptor | TenantConfigInterceptor)
    ]
    assert len(tenant_interceptors) == 2
    inbound = next_
    for interceptor in reversed(tenant_interceptors):
        inbound = interceptor.intercept_activity(inbound)
    return inbound


async def test_a_review_activity_resolves_the_gate_engine_of_the_bundle_its_run_recorded(
    config, monkeypatch
) -> None:
    from services.orchestration.gates.engine_source import load_gate_engine_configs

    configured, source = config
    recorded = _publish(
        configured, source, gate_engine=ENGINE.format(name="adp-policy", digest=DIGEST)
    )
    _publish(configured, source, gate_engine=ENGINE.format(name="adp-policy-next", digest=DIGEST))
    probe = _Probe(lambda: [engine.name for engine in load_gate_engine_configs()])
    inbound = _review_inbound(
        monkeypatch,
        configured,
        probe,
        recorded_tenant="adp",
        bundle=("adp", recorded.to_manifest()),
    )

    await inbound.execute_activity(_input())

    assert "adp-policy" in probe.seen[0]
    assert "adp-policy-next" not in probe.seen[0]


async def test_registry_mirrors_in_a_review_activity_read_the_run_tenants_registries(
    config, monkeypatch
) -> None:
    import os
    from pathlib import Path

    from services.orchestration.gates import registry_mirrors

    configured, source = config
    (Path(os.environ["PLATFORM_ENV_CONFIG_DIR"]) / "registries.yaml").write_text(
        PLATFORM_REGISTRIES
    )
    recorded = _publish(
        configured, source, registries=TENANT_REGISTRIES.format(username="adp-reader")
    )
    _publish(configured, source, registries=TENANT_REGISTRIES.format(username="adp-rotated"))
    probe = _Probe(registry_mirrors.current)
    inbound = _review_inbound(
        monkeypatch,
        configured,
        probe,
        recorded_tenant="adp",
        bundle=("adp", recorded.to_manifest()),
    )

    await inbound.execute_activity(_input())

    assert probe.seen[0].username == "adp-reader"
    assert probe.seen[0].mirrors == {"docker.io": "artifactory.example.gov/docker-remote"}


async def test_a_review_activity_of_another_tenants_run_is_refused(config, monkeypatch) -> None:
    configured, _ = config
    probe = _Probe(tenant_bundle_dir)
    inbound = _review_inbound(monkeypatch, configured, probe, recorded_tenant="beta", bundle=None)

    with pytest.raises(ApplicationError) as raised:
        await inbound.execute_activity(_input())

    assert raised.value.type == TENANT_MISMATCH
    assert raised.value.non_retryable
    assert probe.seen == []
