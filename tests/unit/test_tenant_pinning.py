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
    (env / "tickets.yaml").write_text(TICKETS)
    write_image_policy(env)
    monkeypatch.setenv("PLATFORM_ENV_CONFIG_DIR", str(env))
    source = tmp_path / "source"
    source.mkdir()
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
