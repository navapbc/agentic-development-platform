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
)
from services.orchestration.worker_versioning import prune_drained_versions, set_current_version
from temporalio.api.enums.v1 import VersionDrainageStatus
from temporalio.common import VersioningBehavior
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
    monkeypatch.setenv("WORKER_DEPLOYMENT_NAME", "agent-platform")
    get_settings.cache_clear()
    try:
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with build_worker(env.client, "q-versioned", WORKER_ROLE_ACTIVITY) as worker:
                config = worker.config()
            deployment = config["deployment_config"]
            assert deployment.use_worker_versioning is True
            assert deployment.default_versioning_behavior is VersioningBehavior.PINNED
            assert deployment.version.build_id == "0.3.0.20261028093000"
            assert deployment.version.deployment_name == "agent-platform"
            assert not config["build_id"]
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
            assert config["deployment_config"] is None
            assert not config["build_id"]
    finally:
        get_settings.cache_clear()


async def test_set_current_version_routes_new_work_to_this_build() -> None:
    client = _RecordingClient(queues={"0.3.0.20261028093000": {"q-one", "q-two"}})

    await set_current_version(
        client, _BuildSettings("0.3.0.20261028093000"), ["q-one", "q-two"], poller_timeout=1.0
    )

    assert client.workflow_service.set_calls == [("agent-platform", "0.3.0.20261028093000")]


async def test_set_current_version_waits_for_the_queues_this_worker_polls() -> None:
    client = _RecordingClient(
        queues={"0.3.0.20261028093000": {"q-one"}},
        queues_after_describes=(2, {"0.3.0.20261028093000": {"q-one", "q-two"}}),
    )

    await set_current_version(
        client, _BuildSettings("0.3.0.20261028093000"), ["q-one", "q-two"], poller_timeout=30.0
    )

    assert client.workflow_service.set_calls == [("agent-platform", "0.3.0.20261028093000")]


async def test_set_current_version_refuses_a_build_the_server_never_registers() -> None:
    client = _RecordingClient(queues={})

    with pytest.raises(SystemExit, match="did not register task queues"):
        await set_current_version(
            client, _BuildSettings("0.3.0.20261028093000"), ["q-one"], poller_timeout=1.0
        )


async def test_set_current_version_leaves_a_newer_build_current() -> None:
    client = _RecordingClient(
        queues={"0.3.0.20261028093000": {"q-one"}}, current="0.3.0.20261029093000"
    )

    await set_current_version(
        client, _BuildSettings("0.3.0.20261028093000"), ["q-one"], poller_timeout=1.0
    )

    assert client.workflow_service.set_calls == []


async def test_set_current_version_accepts_a_build_already_current() -> None:
    client = _RecordingClient(
        queues={"0.3.0.20261028093000": {"q-one"}}, current="0.3.0.20261028093000"
    )

    await set_current_version(
        client, _BuildSettings("0.3.0.20261028093000"), ["q-one"], poller_timeout=1.0
    )

    assert client.workflow_service.set_calls == []


async def test_set_current_version_retries_a_token_another_worker_consumed() -> None:
    client = _RecordingClient(
        queues={"0.3.0.20261028093000": {"q-one"}},
        set_errors=[RPCError("conflict token mismatch", RPCStatusCode.FAILED_PRECONDITION, b"")],
    )

    await set_current_version(
        client, _BuildSettings("0.3.0.20261028093000"), ["q-one"], poller_timeout=1.0
    )

    assert len(client.workflow_service.set_calls) == 2


async def test_set_current_version_refuses_to_start_when_the_server_rejects_it() -> None:
    client = _RecordingClient(
        queues={"0.3.0.20261028093000": {"q-one"}},
        set_errors=[RPCError("deployments disabled", RPCStatusCode.PERMISSION_DENIED, b"")],
    )

    with pytest.raises(SystemExit, match="WORKER_VERSIONING_ENABLED=false"):
        await set_current_version(
            client, _BuildSettings("0.3.0.20261028093000"), ["q-one"], poller_timeout=1.0
        )


async def test_prune_drained_versions_deletes_only_the_drained_ones() -> None:
    client = _RecordingClient(
        queues={},
        summaries=[
            ("0.3.0.20261026093000", VersionDrainageStatus.VERSION_DRAINAGE_STATUS_DRAINED),
            ("0.3.0.20261027093000", VersionDrainageStatus.VERSION_DRAINAGE_STATUS_DRAINING),
            ("0.3.0.20261028093000", VersionDrainageStatus.VERSION_DRAINAGE_STATUS_UNSPECIFIED),
        ],
    )

    deleted, kept = await prune_drained_versions(client, _BuildSettings("0.3.0.20261028093000"))

    assert deleted == ["0.3.0.20261026093000"]
    assert kept == []
    assert client.workflow_service.deleted == ["0.3.0.20261026093000"]


async def test_prune_drained_versions_keeps_a_version_the_server_refuses_to_delete() -> None:
    client = _RecordingClient(
        queues={},
        summaries=[
            ("0.3.0.20261026093000", VersionDrainageStatus.VERSION_DRAINAGE_STATUS_DRAINED),
            ("0.3.0.20261027093000", VersionDrainageStatus.VERSION_DRAINAGE_STATUS_DRAINED),
        ],
        delete_errors={
            "0.3.0.20261026093000": RPCError(
                "Version cannot be deleted since it has active pollers.",
                RPCStatusCode.FAILED_PRECONDITION,
                b"",
            )
        },
    )

    deleted, kept = await prune_drained_versions(client, _BuildSettings("0.3.0.20261028093000"))

    assert deleted == ["0.3.0.20261027093000"]
    assert kept == ["0.3.0.20261026093000"]


class _BuildSettings:
    def __init__(self, build_id: str) -> None:
        self.agent_build_id = build_id
        self.worker_deployment_name = "agent-platform"


class _RecordingWorkflowService:
    def __init__(self, outer: _RecordingClient) -> None:
        self._outer = outer
        self.set_calls: list[tuple[str, str]] = []
        self.deleted: list[str] = []
        self.describes = 0

    async def describe_worker_deployment_version(self, request):
        build_id = request.deployment_version.build_id
        self.describes += 1
        threshold, queues = self._outer.queues_after_describes
        listed = queues if self.describes >= threshold else self._outer.queues
        if build_id not in listed:
            raise RPCError("version not found", RPCStatusCode.NOT_FOUND, b"")
        from temporalio.api.deployment.v1 import WorkerDeploymentVersionInfo
        from temporalio.api.workflowservice.v1 import DescribeWorkerDeploymentVersionResponse

        return DescribeWorkerDeploymentVersionResponse(
            worker_deployment_version_info=WorkerDeploymentVersionInfo(
                task_queue_infos=[
                    WorkerDeploymentVersionInfo.VersionTaskQueueInfo(name=name)
                    for name in sorted(listed[build_id])
                ]
            )
        )

    async def describe_worker_deployment(self, request):
        from temporalio.api.deployment.v1 import (
            RoutingConfig,
            VersionDrainageInfo,
            WorkerDeploymentInfo,
            WorkerDeploymentVersion,
        )
        from temporalio.api.workflowservice.v1 import DescribeWorkerDeploymentResponse

        return DescribeWorkerDeploymentResponse(
            conflict_token=b"token",
            worker_deployment_info=WorkerDeploymentInfo(
                name=request.deployment_name,
                routing_config=RoutingConfig(
                    current_deployment_version=WorkerDeploymentVersion(
                        deployment_name=request.deployment_name,
                        build_id=self._outer.current,
                    )
                )
                if self._outer.current
                else RoutingConfig(),
                version_summaries=[
                    WorkerDeploymentInfo.WorkerDeploymentVersionSummary(
                        deployment_version=WorkerDeploymentVersion(
                            deployment_name=request.deployment_name, build_id=build_id
                        ),
                        drainage_info=VersionDrainageInfo(status=status),
                    )
                    for build_id, status in self._outer.summaries
                ],
            ),
        )

    async def set_worker_deployment_current_version(self, request):
        self.set_calls.append((request.deployment_name, request.build_id))
        assert request.ignore_missing_task_queues is True
        if self._outer.set_errors:
            raise self._outer.set_errors.pop(0)
        self._outer.current = request.build_id

    async def delete_worker_deployment_version(self, request):
        build_id = request.deployment_version.build_id
        error = self._outer.delete_errors.get(build_id)
        if error is not None:
            raise error
        self.deleted.append(build_id)


class _RecordingClient:
    def __init__(
        self,
        queues: dict[str, set[str]] | None = None,
        current: str = "",
        set_errors: list[Exception] | None = None,
        summaries: list[tuple[str, int]] | None = None,
        queues_after_describes: tuple[int, dict[str, set[str]]] | None = None,
        delete_errors: dict[str, Exception] | None = None,
    ) -> None:
        self.namespace = "default"
        self.queues = queues or {}
        self.current = current
        self.set_errors = set_errors or []
        self.summaries = summaries or []
        self.queues_after_describes = queues_after_describes or (2**30, {})
        self.delete_errors = delete_errors or {}
        self.workflow_service = _RecordingWorkflowService(self)


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
