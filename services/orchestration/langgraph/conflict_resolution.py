"""Bounded LangGraph stage that classifies rebase conflicts and auto-resolves trivial ones."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

RESOLUTION_CAP = 2

RESOLVED = "resolved"
PAUSED = "paused"
TRIVIAL = "trivial"
MATERIAL = "material"


@dataclass(frozen=True)
class Conflict:
    path: str
    ours: str
    theirs: str
    base: str = ""


@dataclass(frozen=True)
class ConflictResolution:
    status: str
    resolved: dict[str, str] = field(default_factory=dict)
    material: list[str] = field(default_factory=list)
    attempts: int = 0


def _nonblank_lines(text: str) -> list[str]:
    return [line.rstrip() for line in text.splitlines() if line.strip()]


def classify_conflict(conflict: Conflict) -> str:
    ours = _nonblank_lines(conflict.ours)
    theirs = _nonblank_lines(conflict.theirs)
    if ours == theirs:
        return TRIVIAL
    base = _nonblank_lines(conflict.base)
    if ours == base or theirs == base:
        return TRIVIAL
    ours_set, theirs_set = set(ours), set(theirs)
    if ours_set <= theirs_set or theirs_set <= ours_set:
        return TRIVIAL
    return MATERIAL


def _resolve_trivial(conflict: Conflict) -> str:
    ours = _nonblank_lines(conflict.ours)
    theirs = _nonblank_lines(conflict.theirs)
    if ours == theirs:
        return conflict.ours
    base = _nonblank_lines(conflict.base)
    if ours == base:
        return conflict.theirs
    if theirs == base:
        return conflict.ours
    merged = list(ours)
    merged += [line for line in theirs if line not in ours]
    return "\n".join(merged) + "\n"


class _ConflictState(TypedDict):
    conflicts: list[Conflict]
    cap: int
    attempts: int
    resolved: dict[str, str]
    material: list[str]
    status: str


def _classify(state: _ConflictState) -> _ConflictState:
    material = [c.path for c in state["conflicts"] if classify_conflict(c) == MATERIAL]
    return {**state, "material": material}


def _route(state: _ConflictState) -> str:
    if state["material"]:
        return "pause"
    if state["attempts"] >= state["cap"]:
        return "pause"
    return "resolve"


def _resolve(state: _ConflictState) -> _ConflictState:
    resolved = {c.path: _resolve_trivial(c) for c in state["conflicts"]}
    return {**state, "resolved": resolved, "attempts": state["attempts"] + 1, "status": RESOLVED}


def _pause(state: _ConflictState) -> _ConflictState:
    return {**state, "status": PAUSED}


def build_conflict_graph():
    graph = StateGraph(_ConflictState)
    graph.add_node("classify", _classify)
    graph.add_node("resolve", _resolve)
    graph.add_node("pause", _pause)
    graph.add_edge(START, "classify")
    graph.add_conditional_edges("classify", _route, {"resolve": "resolve", "pause": "pause"})
    graph.add_edge("resolve", END)
    graph.add_edge("pause", END)
    return graph.compile()


_GRAPH = build_conflict_graph()


def resolve_conflicts(
    conflicts: list[Conflict], *, cap: int = RESOLUTION_CAP
) -> ConflictResolution:
    final = _GRAPH.invoke(
        {
            "conflicts": conflicts,
            "cap": cap,
            "attempts": 0,
            "resolved": {},
            "material": [],
            "status": "",
        }
    )
    return ConflictResolution(
        status=final["status"],
        resolved=final["resolved"],
        material=final["material"],
        attempts=final["attempts"],
    )
