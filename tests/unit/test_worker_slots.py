"""Slot choice of a two-slot worker deploy, shared by the ECS and EKS deploy paths."""

from __future__ import annotations

from scripts.worker_slots import (
    PinnedRuns,
    SlotPlan,
    SlotRefusal,
    SlotState,
    choose_slots,
    may_stop,
)


class _Check:
    def __init__(self, pinned: dict[str, tuple[str, ...]] | None = None) -> None:
        self.pinned = pinned or {}
        self.builds: list[str] = []

    def __call__(self, build: str) -> list[str]:
        self.builds.append(build)
        return list(self.pinned.get(build, ()))


def test_an_idle_slot_takes_the_new_build_and_the_unpinned_old_slot_stops() -> None:
    check = _Check()
    pinned = PinnedRuns(check)
    slots = {"blue": SlotState(replicas=2, build="0.3.0.1"), "green": SlotState(0, "0.3.0.1")}

    plan = choose_slots(slots, "0.3.0.2", pinned)

    assert plan == SlotPlan(start="green", drain="blue")
    assert may_stop(slots["blue"], pinned)


def test_an_old_slot_with_pinned_runs_keeps_running_while_the_new_build_starts() -> None:
    pinned = PinnedRuns(_Check({"0.3.0.1": ("ticket-7",)}))
    slots = {"blue": SlotState(2, "0.3.0.1"), "green": SlotState(0, "0.3.0.1")}

    plan = choose_slots(slots, "0.3.0.2", pinned)

    assert plan == SlotPlan(start="green", drain="blue")
    assert not may_stop(slots["blue"], pinned)


def test_a_deploy_of_the_running_build_replaces_that_slot_in_place() -> None:
    check = _Check({"0.3.0.1": ("ticket-7",)})
    slots = {"blue": SlotState(2, "0.3.0.1"), "green": SlotState(2, "0.3.0.2")}

    plan = choose_slots(slots, "0.3.0.2", PinnedRuns(check))

    assert plan == SlotPlan(start="green", drain="blue")
    assert check.builds == []


def test_a_deploy_of_the_only_running_build_leaves_nothing_to_drain() -> None:
    slots = {"blue": SlotState(0, "0.3.0.1"), "green": SlotState(2, "0.3.0.2")}

    assert choose_slots(slots, "0.3.0.2", PinnedRuns(_Check())) == SlotPlan("green", None)


def test_two_older_builds_with_pinned_runs_refuse_and_name_each_build_and_run() -> None:
    pinned = PinnedRuns(_Check({"0.3.0.0": ("ticket-3",), "0.3.0.1": ("ticket-7", "ticket-8")}))
    slots = {"blue": SlotState(2, "0.3.0.0"), "green": SlotState(2, "0.3.0.1")}

    refusal = choose_slots(slots, "0.3.0.2", pinned)

    assert refusal == SlotRefusal(
        pinned={"0.3.0.0": ("ticket-3",), "0.3.0.1": ("ticket-7", "ticket-8")}
    )
    assert isinstance(refusal, SlotRefusal)
    assert refusal.describe() == (
        "build 0.3.0.0 pinned by ticket-3 and build 0.3.0.1 pinned by ticket-7, ticket-8"
    )


def test_a_third_build_takes_the_slot_whose_build_has_no_pinned_runs() -> None:
    pinned = PinnedRuns(_Check({"0.3.0.1": ("ticket-7",)}))
    slots = {"blue": SlotState(2, "0.3.0.0"), "green": SlotState(2, "0.3.0.1")}

    assert choose_slots(slots, "0.3.0.2", pinned) == SlotPlan(start="blue", drain="green")


def test_the_pinned_check_runs_once_per_build_per_deploy() -> None:
    check = _Check({"0.3.0.1": ("ticket-7",)})
    pinned = PinnedRuns(check)
    workflow = {"blue": SlotState(2, "0.3.0.0"), "green": SlotState(2, "0.3.0.1")}
    review = {"blue": SlotState(1, "0.3.0.1"), "green": SlotState(1, "0.3.0.0")}

    choose_slots(workflow, "0.3.0.2", pinned)
    choose_slots(review, "0.3.0.2", pinned)
    may_stop(workflow["green"], pinned)

    assert sorted(check.builds) == ["0.3.0.0", "0.3.0.1"]
