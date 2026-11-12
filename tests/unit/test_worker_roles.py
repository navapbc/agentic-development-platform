"""The review worker role and the review-vs-activity task split."""

from __future__ import annotations

from services.orchestration.worker import (
    _ACTIVITIES,
    _REVIEW_ACTIVITIES,
    WORKER_ROLE_ACTIVITY,
    WORKER_ROLE_REVIEW,
    _all_activities,
    build_worker,
)
from temporalio.testing import WorkflowEnvironment


def _activity_names(activities) -> set[str]:
    names: set[str] = set()
    for activity in activities:
        defn = getattr(activity, "__temporal_activity_definition", None)
        names.add(defn.name if defn is not None else getattr(activity, "__name__", str(activity)))
    return names


def test_review_activities_are_the_review_gates_and_gate_evaluator():
    names = _activity_names(_REVIEW_ACTIVITIES)
    assert names == {
        "evaluate_gate",
        "review_plan",
        "review_code",
        "review_security",
        "verify_acceptance",
    }


def test_activity_role_excludes_the_review_gates():
    names = _activity_names(_ACTIVITIES)
    assert "evaluate_gate" in names
    assert not {"review_plan", "review_code", "review_security", "verify_acceptance"} & names


def test_all_activities_union_is_deduplicated():
    combined = _all_activities()
    assert len(combined) == len({id(activity) for activity in combined})
    assert _activity_names(combined) >= _activity_names(_ACTIVITIES) | _activity_names(
        _REVIEW_ACTIVITIES
    )


async def test_build_worker_review_role_registers_only_review_activities():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with build_worker(env.client, "q-review", WORKER_ROLE_REVIEW) as worker:
            config = worker.config()
        assert config.get("activities")
        assert not config.get("workflows")


async def test_review_and_activity_roles_use_distinct_queues():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        review = build_worker(env.client, "review-queue", WORKER_ROLE_REVIEW)
        activity = build_worker(env.client, "activity-queue", WORKER_ROLE_ACTIVITY)
        async with review, activity:
            assert review.task_queue == "review-queue"
            assert activity.task_queue == "activity-queue"
