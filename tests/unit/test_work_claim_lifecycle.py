"""The work claim reaches a terminal state on every orchestrator outcome."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from services.coordination.claims import (
    CLAIMED,
    COMPLETED,
    RELEASED,
    InMemoryWorkClaimStore,
    WorkUnit,
    claim_id_for,
)
from services.orchestration.temporal_activities.implementation import (
    ClaimRequest,
    ClaimResult,
    FinalizeClaimRequest,
    FinalizeClaimResult,
    ImplementationRequest,
    ImplementationResult,
    ResetCoordinationRequest,
    ResetCoordinationResult,
)
from services.orchestration.temporal_activities.persistence import AuditEvent
from services.orchestration.temporal_workflows.branch_sync import BranchSyncWorkflow
from services.orchestration.temporal_workflows.draft_pull_request import (
    CI_FAILED,
    PR_CLOSED_UNMERGED,
    PR_MERGED,
    PR_PROMOTED,
    PROMOTION_CANCELLED,
    DraftPullRequestInput,
    DraftPullRequestWorkflow,
)
from services.orchestration.temporal_workflows.implementation import ImplementationWorkflow
from services.orchestration.temporal_workflows.pull_request import PullRequestWorkflow
from services.orchestration.temporal_workflows.review import PlanReviewWorkflow
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions
from tests.unit.test_draft_pull_request_workflow import FAILURE, _make_activities

_TASK_ID = "T-1"
_WORK_UNIT = "unit-1"
_CLAIM_ID = claim_id_for(_TASK_ID, _WORK_UNIT)


def _claim_activities(store: InMemoryWorkClaimStore, owners: list[str]):
    @activity.defn(name="claim_work_unit")
    async def fake_claim(req: ClaimRequest) -> ClaimResult:
        owner = req.owner_workflow_id or activity.info().workflow_id
        owners.append(owner)
        outcome = await store.try_claim(
            WorkUnit(
                task_id=req.task_id,
                work_unit=req.work_unit,
                execution_id=req.execution_id,
                agent_id=req.agent_id,
                file_impact=tuple(req.file_impact),
                workflow_id=owner,
            )
        )
        return ClaimResult(
            claim_id=outcome.claim_id,
            won=outcome.won,
            claim_version=outcome.claim_version,
            owner_workflow_id=owner if outcome.won else "",
        )

    @activity.defn(name="reset_task_coordination")
    async def fake_reset(_req: ResetCoordinationRequest) -> ResetCoordinationResult:
        return ResetCoordinationResult(sequence_reset=True)

    @activity.defn(name="finalize_work_claim")
    async def fake_finalize(req: FinalizeClaimRequest) -> FinalizeClaimResult:
        claim_id = claim_id_for(req.task_id, req.work_unit)
        state = COMPLETED if req.completed else RELEASED
        if not req.owner_workflow_id:
            return FinalizeClaimResult(claim_id=claim_id, finalized=False, state=state)
        if req.completed:
            finalized = await store.complete(claim_id, owner_workflow_id=req.owner_workflow_id)
        else:
            finalized = await store.release(claim_id, owner_workflow_id=req.owner_workflow_id)
        return FinalizeClaimResult(claim_id=claim_id, finalized=finalized, state=state)

    return [fake_claim, fake_reset, fake_finalize]


def _activities(store: InMemoryWorkClaimStore, owners: list[str], overrides: dict, extra=()):
    ordered = [
        *_make_activities(None, 1, [], [], [], **overrides),
        *_claim_activities(store, owners),
        *extra,
    ]
    by_name = {fn.__temporal_activity_definition.name: fn for fn in ordered}
    return list(by_name.values())


def _input(**overrides) -> DraftPullRequestInput:
    fields = {
        "task_id": _TASK_ID,
        "execution_id": "exec-1",
        "repo": "agentic-demo-service",
        "base": "main",
        "base_sha": "a" * 40,
        "task": "Implement the change",
        "issue_key": _TASK_ID,
        "work_unit": _WORK_UNIT,
        "file_impact": ["feature.py"],
        "stored_issue": {"summary": "Implement the change"},
    }
    fields.update(overrides)
    return DraftPullRequestInput(**fields)


def _worker(env, task_queue: str, activities) -> Worker:
    return Worker(
        env.client,
        task_queue=task_queue,
        workflows=[
            DraftPullRequestWorkflow,
            ImplementationWorkflow,
            BranchSyncWorkflow,
            PullRequestWorkflow,
            PlanReviewWorkflow,
        ],
        workflow_runner=SandboxedWorkflowRunner(
            restrictions=SandboxRestrictions.default.with_passthrough_modules("beartype")
        ),
        activities=activities,
    )


async def _await_park(handle, stage: str) -> dict[str, str]:
    state: dict[str, str] = {}
    for _ in range(50):
        state = await handle.query(DraftPullRequestWorkflow.suspension_state)
        if state.get("stage") == stage:
            return state
        await asyncio.sleep(0.05)
    return state


async def _live_owner_store() -> InMemoryWorkClaimStore:
    async def owner_active(_workflow_id: str) -> bool:
        return True

    return InMemoryWorkClaimStore(owner_active=owner_active)


async def _run(
    *,
    workflow_input=None,
    signal=None,
    park_stage: str = "",
    extra_activities=(),
    at_park=None,
    store=None,
    expect_failure: bool = False,
    **overrides,
):
    store = store if store is not None else InMemoryWorkClaimStore()
    owners: list[str] = []
    parent_workflow_id = ""
    activities = _activities(store, owners, overrides, extra_activities)
    params = workflow_input or _input()

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"claim-{uuid.uuid4().hex[:8]}"
        parent_workflow_id = f"wf-{uuid.uuid4().hex[:8]}"
        async with _worker(env, task_queue, activities):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                params,
                id=parent_workflow_id,
                task_queue=task_queue,
            )
            if at_park is not None:
                await _await_park(handle, park_stage)
                await at_park(store)
            if signal is not None:
                if at_park is None:
                    await _await_park(handle, park_stage)
                await handle.signal(signal[0], args=list(signal[1]))
            if expect_failure:
                result = None
                with pytest.raises(WorkflowFailureError):
                    await handle.result()
            else:
                result = await handle.result()
    return result, store, owners, parent_workflow_id


def _claim_state(store: InMemoryWorkClaimStore) -> str:
    return str(store._claims[_CLAIM_ID]["state"])


async def _reclaim(store: InMemoryWorkClaimStore):
    return await store.try_claim(
        WorkUnit(
            task_id=_TASK_ID,
            work_unit=_WORK_UNIT,
            execution_id="exec-2",
            agent_id="agent-b",
            workflow_id="wf-later",
        )
    )


async def test_promoted_run_completes_the_claim():
    result, store, _owners, _parent = await _run()

    assert result.status == PR_PROMOTED
    assert _claim_state(store) == COMPLETED


async def test_the_claim_owner_is_the_parent_orchestrator_not_the_implementation_child():
    _result, store, owners, parent_workflow_id = await _run()

    assert owners == [parent_workflow_id]
    assert not owners[0].endswith("-impl")
    assert store._claims[_CLAIM_ID]["owner_workflow_id"] == parent_workflow_id


async def test_human_abandoned_repair_releases_the_claim():
    result, store, _owners, _parent = await _run(
        workflow_input=_input(max_repair_attempts=1),
        ci_result=FAILURE,
        signal=("submit_repair_decision", (False, "release-manager")),
        park_stage="repair",
    )

    assert result.status == CI_FAILED
    assert result.terminal_reason == "human_abandoned"
    assert _claim_state(store) == RELEASED


async def test_a_cancelled_promotion_releases_the_claim():
    result, store, _owners, _parent = await _run(
        recheck_requires_pause=True,
        recheck_category="scope_changed",
    )

    assert result.status == PROMOTION_CANCELLED
    assert _claim_state(store) == RELEASED


async def test_an_unmerged_pull_request_releases_the_claim():
    result, store, _owners, _parent = await _run(
        workflow_input=_input(await_pr_close=True),
        signal=("submit_pr_closed", (False,)),
        park_stage="pr_close",
    )

    assert result.status == PR_CLOSED_UNMERGED
    assert _claim_state(store) == RELEASED


async def test_a_merged_pull_request_completes_the_claim():
    result, store, _owners, _parent = await _run(
        workflow_input=_input(await_pr_close=True),
        signal=("submit_pr_closed", (True,)),
        park_stage="pr_close",
    )

    assert result.status == PR_MERGED
    assert _claim_state(store) == COMPLETED


async def test_a_completed_claim_is_not_reclaimable():
    _result, store, _owners, _parent = await _run()

    reclaim = await _reclaim(store)

    assert reclaim.won is False
    assert _claim_state(store) == COMPLETED


async def test_a_released_claim_is_reclaimable():
    _result, store, _owners, _parent = await _run(
        recheck_requires_pause=True,
        recheck_category="scope_changed",
    )

    reclaim = await _reclaim(store)

    assert reclaim.won is True
    assert _claim_state(store) == CLAIMED


def _stage_raises():
    @activity.defn(name="run_implementation_stage")
    async def fake_stage(_req: ImplementationRequest) -> ImplementationResult:
        raise ApplicationError("sandbox could not be provisioned", non_retryable=True)

    return [fake_stage]


def _stage_fails():
    @activity.defn(name="run_implementation_stage")
    async def fake_stage(_req: ImplementationRequest) -> ImplementationResult:
        return ImplementationResult(
            status="failed",
            terminal_reason="agent_gave_up",
            tool_calls=1,
            elapsed_seconds=1.0,
            cost=0.1,
            tokens=10,
            llm_calls=1,
            branch="",
            commit_sha="",
            diff_hash="",
            model_id="coding-model",
            prompt_version="implementation/v1",
        )

    return [fake_stage]


async def test_a_raising_implementation_stage_releases_the_claim():
    _result, store, _owners, _parent = await _run(
        extra_activities=_stage_raises(),
        expect_failure=True,
    )

    assert _claim_state(store) == RELEASED
    assert (await _reclaim(store)).won is True


async def test_a_failed_implementation_releases_the_claim():
    result, store, _owners, _parent = await _run(extra_activities=_stage_fails())

    assert result.status == "impl_failed"
    assert _claim_state(store) == RELEASED


async def test_the_claim_is_held_while_a_human_decides_on_a_repair():
    held: dict[str, object] = {}

    async def inspect(store: InMemoryWorkClaimStore) -> None:
        held["state"] = _claim_state(store)
        held["rival_won"] = (await _reclaim(store)).won

    result, store, _owners, _parent = await _run(
        workflow_input=_input(max_repair_attempts=1),
        ci_result=FAILURE,
        store=await _live_owner_store(),
        at_park=inspect,
        signal=("submit_repair_decision", (False, "release-manager")),
        park_stage="repair",
    )

    assert held["state"] == CLAIMED
    assert held["rival_won"] is False
    assert result.terminal_reason == "human_abandoned"
    assert _claim_state(store) == RELEASED


async def test_resetting_task_coordination_does_not_touch_the_work_claim(monkeypatch):
    import services.coordination.claims as claims_module
    import services.coordination.sequencing as sequencing_module
    from services.orchestration.temporal_activities.implementation import (
        reset_task_coordination,
    )

    reset_tasks: list[str] = []

    class _SpyGate:
        async def reset(self, task_id: str) -> int:
            reset_tasks.append(task_id)
            return 1

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("reset_task_coordination must not reach the work claim store")

    monkeypatch.setattr(sequencing_module, "PostgresSequenceGate", _SpyGate)
    monkeypatch.setattr(claims_module, "PostgresWorkClaimStore", _forbidden)

    result = await reset_task_coordination(
        ResetCoordinationRequest(task_id=_TASK_ID, work_unit=_WORK_UNIT)
    )

    assert result.sequence_reset is True
    assert reset_tasks == [_TASK_ID]


def _audit_collector(events: list[AuditEvent]):
    @activity.defn(name="record_audit_event")
    async def fake_audit(event: AuditEvent) -> str:
        events.append(event)
        return event.event_id

    return [fake_audit]


async def test_settling_a_claim_records_an_audit_event():
    events: list[AuditEvent] = []

    _result, _store, _owners, _parent = await _run(extra_activities=_audit_collector(events))

    settled = [e for e in events if e.event_type == "WORK_UNIT_SETTLED"]
    assert len(settled) == 1
    assert settled[0].decision == COMPLETED
    assert settled[0].reason is None


async def test_a_claim_that_cannot_be_settled_is_recorded_as_unsettled():
    events: list[AuditEvent] = []

    @activity.defn(name="finalize_work_claim")
    async def fake_finalize(req: FinalizeClaimRequest) -> FinalizeClaimResult:
        raise ApplicationError("coordination database unreachable", non_retryable=True)

    _result, _store, _owners, _parent = await _run(
        extra_activities=[*_audit_collector(events), fake_finalize]
    )

    settled = [e for e in events if e.event_type == "WORK_UNIT_SETTLED"]
    assert len(settled) == 1
    assert settled[0].decision == "not_settled"
    assert "did not complete" in settled[0].reason


async def test_a_claim_no_longer_held_names_the_owner_and_the_work_unit():
    events: list[AuditEvent] = []

    @activity.defn(name="finalize_work_claim")
    async def fake_finalize(req: FinalizeClaimRequest) -> FinalizeClaimResult:
        return FinalizeClaimResult(
            claim_id=claim_id_for(req.task_id, req.work_unit), finalized=False, state=RELEASED
        )

    _result, _store, _owners, parent = await _run(
        extra_activities=[*_audit_collector(events), fake_finalize]
    )

    settled = [e for e in events if e.event_type == "WORK_UNIT_SETTLED"]
    assert len(settled) == 1
    assert settled[0].decision == "not_settled"
    assert parent in settled[0].reason
    assert _WORK_UNIT in settled[0].reason
