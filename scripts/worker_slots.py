"""Choose which slot of a two-slot worker takes a new build and which slot is left to drain.

A worker runs in two slots so that a new build starts in one slot while the other keeps serving
the runs pinned to the previous build. The decision reads only slot states and the pinned runs of
each build, so the ECS and EKS deploy paths share it. This module imports only the standard
library, because the eks-deployer image copies it without the rest of the repository.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class SlotState:
    replicas: int
    build: str


@dataclass(frozen=True)
class SlotPlan:
    start: str
    drain: str | None


@dataclass(frozen=True)
class SlotRefusal:
    """Both slots run older builds with pinned runs, so the new build has no slot."""

    pinned: dict[str, tuple[str, ...]]

    def describe(self) -> str:
        return " and ".join(
            f"build {build} pinned by {', '.join(runs)}" for build, runs in self.pinned.items()
        )


class PinnedRuns:
    """The execution ids pinned to each build, checked once per build for one deploy."""

    def __init__(self, check: Callable[[str], Sequence[str]]) -> None:
        self._check = check
        self._checked: dict[str, tuple[str, ...]] = {}

    def __call__(self, build: str) -> tuple[str, ...]:
        if build not in self._checked:
            self._checked[build] = tuple(self._check(build))
        return self._checked[build]


def build_id_of(tag: str) -> str:
    return tag.rsplit("-", 2)[0]


def image_build(image: str) -> str:
    """Return the build ID of a published image reference, `repository:tag@digest`."""
    return build_id_of(image.split("@")[0].rsplit(":", 1)[1])


def choose_slots(
    slots: Mapping[str, SlotState], target_build: str, pinned: PinnedRuns
) -> SlotPlan | SlotRefusal:
    """Return the slot that takes `target_build` and the slot left to drain, or a refusal.

    A slot running the target build takes it in place. Otherwise the idle slot takes the new
    build and the running slot, if any, is left to drain. With both slots running older builds,
    a slot whose build has no pinned runs takes the new build.
    """
    running = [slot for slot, state in slots.items() if state.replicas > 0]
    for slot in running:
        if slots[slot].build == target_build:
            return SlotPlan(slot, next((other for other in running if other != slot), None))
    if len(running) == len(slots):
        runs = {slot: pinned(slots[slot].build) for slot in running}
        free = [slot for slot in running if not runs[slot]]
        if not free:
            return SlotRefusal({slots[slot].build: runs[slot] for slot in running})
        return SlotPlan(free[0], next(slot for slot in running if slot != free[0]))
    idle = next(slot for slot in slots if slot not in running)
    return SlotPlan(idle, running[0] if running else None)


def may_stop(slot: SlotState, pinned: PinnedRuns) -> bool:
    """Report whether a slot left to drain may scale to zero, which needs no runs pinned to it."""
    return not pinned(slot.build)
