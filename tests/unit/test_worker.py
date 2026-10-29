"""Role selection and worker construction for the Temporal worker entry point."""

from __future__ import annotations

import pytest
from scripts.config import get_settings
from services.orchestration.worker import (
    WORKER_ROLE_ACTIVITY,
    WORKER_ROLE_ALL,
    WORKER_ROLE_WORKFLOW,
    _resolve_role,
    build_worker,
    register_build_id,
)
from temporalio.service import RPCError, RPCStatusCode
from temporalio.testing import WorkflowEnvironment


def test_resolve_role_defaults_to_all(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WORKER_ROLE", raising=False)
    assert _resolve_role() == WORKER_ROLE_ALL


def test_resolve_role_normalizes_case_and_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKER_ROLE", "  Workflow  ")
    assert _resolve_role() == WORKER_ROLE_WORKFLOW


def test_resolve_role_rejects_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKER_ROLE", "bogus")
    with pytest.raises(SystemExit):
        _resolve_role()


async def test_build_worker_workflow_role_registers_only_workflows() -> None:
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with build_worker(env.client, "q-workflow", WORKER_ROLE_WORKFLOW) as worker:
            config = worker.config()
        assert config["workflows"]
        assert not config["activities"]


async def test_build_worker_activity_role_registers_only_activities() -> None:
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with build_worker(env.client, "q-activity", WORKER_ROLE_ACTIVITY) as worker:
            config = worker.config()
        assert config["activities"]
        assert not config["workflows"]


async def test_build_worker_all_role_registers_both() -> None:
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with build_worker(env.client, "q-all", WORKER_ROLE_ALL) as worker:
            config = worker.config()
        assert config["workflows"]
        assert config["activities"]


async def test_build_worker_pins_the_build_id_when_versioning_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKER_VERSIONING_ENABLED", "true")
    monkeypatch.setenv("AGENT_BUILD_ID", "0.3.0.20261028093000")
    get_settings.cache_clear()
    try:
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with build_worker(env.client, "q-versioned", WORKER_ROLE_ACTIVITY) as worker:
                config = worker.config()
            assert config["use_worker_versioning"] is True
            assert config["build_id"] == "0.3.0.20261028093000"
    finally:
        get_settings.cache_clear()


async def test_build_worker_leaves_versioning_off_when_the_setting_is_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKER_VERSIONING_ENABLED", "false")
    get_settings.cache_clear()
    try:
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with build_worker(env.client, "q-unversioned", WORKER_ROLE_ACTIVITY) as worker:
                config = worker.config()
            assert not config["use_worker_versioning"]
            assert not config["build_id"]
    finally:
        get_settings.cache_clear()


async def test_register_build_id_makes_the_build_the_default_of_every_queue() -> None:
    client = _RecordingClient()

    await register_build_id(client, ["q-one", "q-two"], _BuildSettings("0.3.0.20261028093000"))

    assert [queue for queue, _ in client.calls] == ["q-one", "q-two"]
    assert {op.build_id for _, op in client.calls} == {"0.3.0.20261028093000"}


async def test_register_build_id_leaves_a_build_the_queue_already_routes_to_alone() -> None:
    client = _RecordingClient(listed={"q-one": {"0.3.0.20261028093000", "0.3.0.20261028094000"}})

    await register_build_id(client, ["q-one"], _BuildSettings("0.3.0.20261028093000"))

    assert client.calls == []


async def test_register_build_id_accepts_a_queue_a_concurrent_worker_registered_first() -> None:
    client = _RecordingClient(
        error=RPCError("version already exists", RPCStatusCode.INVALID_ARGUMENT, b""),
        listed_after_error={"q-one": {"0.3.0.20261028093000"}},
    )

    await register_build_id(client, ["q-one"], _BuildSettings("0.3.0.20261028093000"))

    assert [queue for queue, _ in client.calls] == ["q-one"]


async def test_register_build_id_refuses_to_start_a_worker_the_server_cannot_route_to() -> None:
    client = _RecordingClient(error=RPCError("disabled", RPCStatusCode.PERMISSION_DENIED, b""))

    with pytest.raises(SystemExit, match="WORKER_VERSIONING_ENABLED=false"):
        await register_build_id(client, ["q-one"], _BuildSettings("0.3.0.20261028093000"))


async def test_register_build_id_names_the_version_set_ceiling_when_the_queue_is_full() -> None:
    client = _RecordingClient(
        error=RPCError(
            "update would exceed number of compatible version sets permitted in namespace "
            "dynamic config (101/100)",
            RPCStatusCode.FAILED_PRECONDITION,
            b"",
        )
    )

    with pytest.raises(SystemExit, match="limit.versionCompatibleSetLimitPerQueue"):
        await register_build_id(client, ["q-one"], _BuildSettings("0.3.0.20261028093000"))


class _BuildSettings:
    def __init__(self, build_id: str) -> None:
        self.agent_build_id = build_id


class _RecordingClient:
    def __init__(
        self,
        error: Exception | None = None,
        listed: dict[str, set[str]] | None = None,
        listed_after_error: dict[str, set[str]] | None = None,
    ) -> None:
        self.calls: list[tuple[str, object]] = []
        self._error = error
        self._listed = listed or {}
        self._listed_after_error = listed_after_error or {}

    async def update_worker_build_id_compatibility(self, task_queue, operation) -> None:
        self.calls.append((task_queue, operation))
        if self._error is not None:
            self._listed = self._listed_after_error
            raise self._error

    async def get_worker_build_id_compatibility(self, task_queue) -> object:
        return _FakeVersionSets(self._listed.get(task_queue, set()))


class _FakeVersionSets:
    def __init__(self, build_ids: set[str]) -> None:
        self.version_sets = [_FakeVersionSet(build_id) for build_id in sorted(build_ids)]


class _FakeVersionSet:
    def __init__(self, build_id: str) -> None:
        self.build_ids = [build_id]


class _FakeCred:
    def __init__(self, auth_mode: str) -> None:
        self.auth_mode = auth_mode


class _FakeResolver:
    def __init__(self, creds: dict) -> None:
        self._creds = creds

    def projects(self) -> set[str]:
        return set(self._creds)

    def resolve(self, project: str):
        return self._creds.get(project)


class _FakeSettings:
    jira_acli_path = ""
    ticket_backend = "jira"


def test_warm_acli_auth_authenticates_a_cloud_project(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.integration.jira import acli_auth, credentials
    from services.orchestration.worker import _warm_acli_auth

    calls: list = []
    resolver = _FakeResolver(
        {"DATA": _FakeCred("datacenter"), "ADSER": _FakeCred(credentials.CLOUD)}
    )
    monkeypatch.setattr(credentials, "build_jira_credential_resolver", lambda _s: resolver)
    monkeypatch.setattr(
        acli_auth, "ensure_acli_authenticated", lambda cred, *, acli_path: calls.append(cred)
    )

    _warm_acli_auth(_FakeSettings())

    assert len(calls) == 1
    assert calls[0].auth_mode == credentials.CLOUD


def test_warm_acli_auth_is_best_effort_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.integration.jira import credentials
    from services.orchestration.worker import _warm_acli_auth

    def _boom(_s):
        raise RuntimeError("resolver down")

    monkeypatch.setattr(credentials, "build_jira_credential_resolver", _boom)

    _warm_acli_auth(_FakeSettings())
