"""Hermetic tests for the load_gate_plan activity's review selection."""

from __future__ import annotations

from services.orchestration.gates import config as gates_config
from services.orchestration.gates.config import parse_gates_config
from services.orchestration.temporal_activities.gates import load_gate_plan


def _patch_config(monkeypatch, raw) -> None:
    config = parse_gates_config(raw)
    monkeypatch.setattr(gates_config, "load_gates_config", lambda *a, **k: config)


async def test_plan_selects_review_gates_in_order_and_excludes_scanners(monkeypatch):
    _patch_config(monkeypatch, {"profile": "government"})

    plan = await load_gate_plan()

    assert [node.name for node in plan.review] == [
        "code-review",
        "security-review",
        "acceptance",
    ]
    assert [node.name for node in plan.scanner] == ["semgrep", "trivy"]
    assert plan.fail_fast_review is False
    assert plan.fail_fast_scanner is False


async def test_plan_drops_a_disabled_review_gate(monkeypatch):
    _patch_config(
        monkeypatch,
        {
            "profile": "government",
            "gates": [{"name": "security-review", "enabled": False}],
        },
    )

    plan = await load_gate_plan()

    assert [node.name for node in plan.review] == ["code-review", "acceptance"]


async def test_plan_reflects_review_fail_fast(monkeypatch):
    _patch_config(
        monkeypatch,
        {"profile": "private", "fail_fast": {"review": True}},
    )

    plan = await load_gate_plan()

    assert plan.fail_fast_review is True


async def test_plan_threads_on_block_to_the_nodes(monkeypatch):
    _patch_config(
        monkeypatch,
        {
            "profile": "government",
            "gates": [{"name": "security-review", "on_block": "pause_for_human"}],
        },
    )

    plan = await load_gate_plan()

    by_name = {node.name: node for node in plan.review}
    assert by_name["security-review"].on_block == "pause_for_human"
    assert by_name["code-review"].on_block == "fail_closed"


async def test_plan_surfaces_the_plan_human_gate(monkeypatch):
    _patch_config(
        monkeypatch,
        {"profile": "private", "gates": [{"name": "plan", "enabled": True}]},
    )

    plan = await load_gate_plan()

    assert plan.plan_pause is True
    assert all(node.name != "plan" for node in plan.review)
    assert all(node.name != "plan" for node in plan.scanner)


async def test_plan_pause_is_off_by_default(monkeypatch):
    _patch_config(monkeypatch, {"profile": "private"})

    plan = await load_gate_plan()

    assert plan.plan_pause is False
