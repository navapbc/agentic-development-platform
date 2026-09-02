"""The lint-repair path applies ruff's safe auto-fixes deterministically."""

from __future__ import annotations

import os
import sys

from services.orchestration.temporal_activities.repair import _autofix_lint


def _with_ruff_on_path(monkeypatch) -> None:
    bindir = os.path.dirname(sys.executable)
    monkeypatch.setenv("PATH", bindir + os.pathsep + os.environ.get("PATH", ""))


async def test_autofix_lint_removes_a_safe_fixable_violation(tmp_path, monkeypatch):
    _with_ruff_on_path(monkeypatch)
    module = tmp_path / "sample.py"
    module.write_text("import os\n\nVALUE = 1\n", encoding="utf-8")

    ran = await _autofix_lint(str(tmp_path))

    assert ran is True
    assert "import os" not in module.read_text(encoding="utf-8")


async def test_autofix_lint_reports_false_when_ruff_is_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "")

    ran = await _autofix_lint(str(tmp_path))

    assert ran is False
