"""Tests for the LangGraph conflict-resolution stage."""

from __future__ import annotations

from services.orchestration.langgraph.conflict_resolution import (
    MATERIAL,
    PAUSED,
    RESOLVED,
    TRIVIAL,
    Conflict,
    classify_conflict,
    resolve_conflicts,
)


def test_identical_sides_classify_trivial():
    conflict = Conflict(path="a.py", ours="x = 1\n", theirs="x = 1\n")
    assert classify_conflict(conflict) == TRIVIAL


def test_pure_addition_superset_classifies_trivial():
    conflict = Conflict(
        path="imports.py",
        ours="import os\nimport sys\n",
        theirs="import os\nimport sys\nimport json\n",
        base="import os\n",
    )
    assert classify_conflict(conflict) == TRIVIAL


def test_divergent_edits_classify_material():
    conflict = Conflict(
        path="calc.py",
        ours="rate = 0.1\n",
        theirs="rate = 0.9\n",
        base="rate = 0.5\n",
    )
    assert classify_conflict(conflict) == MATERIAL


def test_trivial_conflicts_auto_resolve():
    conflicts = [
        Conflict(path="a.py", ours="x = 1\n", theirs="x = 1\n"),
        Conflict(
            path="imports.py",
            ours="import os\n",
            theirs="import os\nimport json\n",
            base="import os\n",
        ),
    ]
    resolution = resolve_conflicts(conflicts)
    assert resolution.status == RESOLVED
    assert set(resolution.resolved) == {"a.py", "imports.py"}
    assert "import json" in resolution.resolved["imports.py"]
    assert resolution.material == []


def test_material_conflict_pauses():
    conflicts = [
        Conflict(path="calc.py", ours="rate = 0.1\n", theirs="rate = 0.9\n", base="rate = 0.5\n"),
    ]
    resolution = resolve_conflicts(conflicts)
    assert resolution.status == PAUSED
    assert resolution.material == ["calc.py"]


def test_resolution_cap_exhaustion_pauses():
    conflicts = [Conflict(path="a.py", ours="x = 1\n", theirs="x = 1\n")]
    resolution = resolve_conflicts(conflicts, cap=0)
    assert resolution.status == PAUSED
