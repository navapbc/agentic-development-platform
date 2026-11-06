"""Exit codes of the pinned-execution report the deploy step reads."""

from __future__ import annotations

from scripts import pinned_executions


async def _none(build_id: str) -> list[str]:
    return []


async def _two(build_id: str) -> list[str]:
    return ["draft-pr-ADP-1", "draft-pr-ADP-2"]


def test_a_build_without_pinned_executions_exits_zero(monkeypatch, capsys) -> None:
    monkeypatch.setattr(pinned_executions, "_pinned", _none)

    assert pinned_executions.main(["0.3.0.1"]) == 0
    assert "no running execution is pinned to 0.3.0.1" in capsys.readouterr().out


def test_a_build_with_pinned_executions_lists_them(monkeypatch, capsys) -> None:
    monkeypatch.setattr(pinned_executions, "_pinned", _two)

    assert pinned_executions.main(["0.3.0.1"]) == pinned_executions.PINNED_EXIT_CODE
    assert "draft-pr-ADP-2" in capsys.readouterr().out
