"""Unit tests for the coverage-based related-tests provider."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from services.repository_intelligence.normalization.models import (
    Location,
    RepoRef,
    Source,
)
from services.repository_intelligence.providers.coverage.coverage_provider import (
    CoverageProvider,
    SymbolSpan,
    build_test_index,
    coverage_map_from_json,
    related_tests_from_coverage,
    resolve_symbol_span,
)

_PROJECT = "agentic-demo-service"

_coverage_available = importlib.util.find_spec("coverage") is not None
requires_coverage = pytest.mark.skipif(
    not _coverage_available, reason="coverage.py must be importable"
)


def _write_sample_repo(root: Path) -> None:
    (root / "app").mkdir()
    (root / "app" / "__init__.py").write_text("", encoding="utf-8")
    (root / "app" / "calc.py").write_text(
        "def add(a, b):\n    total = a + b\n    return total\n\n\n"
        "def unused(a, b):\n    return a - b\n",
        encoding="utf-8",
    )
    (root / "tests").mkdir()
    (root / "tests" / "test_calc.py").write_text(
        "from app.calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\npythonpath = ["."]\n',
        encoding="utf-8",
    )


def test_coverage_map_from_json_normalizes_contexts_and_drops_empty():
    payload = {
        "files": {
            "app/calc.py": {
                "contexts": {
                    "2": ["", "run|test_calc.test_add"],
                    "3": ["test_calc.test_add"],
                    "6": [""],
                }
            }
        }
    }

    coverage_map = coverage_map_from_json(payload)

    assert coverage_map == {
        "app/calc.py": {
            2: frozenset({"test_calc.test_add"}),
            3: frozenset({"test_calc.test_add"}),
        }
    }


def test_resolve_symbol_span_for_function(tmp_path):
    _write_sample_repo(tmp_path)

    span = resolve_symbol_span(tmp_path, "app.calc.add")

    assert span == SymbolSpan(path="app/calc.py", start_line=1, end_line=3)


def test_resolve_symbol_span_for_method(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "store.py").write_text(
        "class Store:\n    def add(self, item):\n        self._items.append(item)\n"
        "        return item\n",
        encoding="utf-8",
    )

    span = resolve_symbol_span(tmp_path, "pkg.store.Store.add")

    assert span == SymbolSpan(path="pkg/store.py", start_line=2, end_line=4)


def test_resolve_symbol_span_includes_decorator_lines(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "app" / "main.py").write_text(
        "import functools\n\n\n@functools.cache\ndef compute(value):\n    return value * 2\n",
        encoding="utf-8",
    )

    span = resolve_symbol_span(tmp_path, "app.main.compute")

    assert span == SymbolSpan(path="app/main.py", start_line=4, end_line=6)


def test_resolve_symbol_span_returns_none_for_missing_symbol(tmp_path):
    _write_sample_repo(tmp_path)

    assert resolve_symbol_span(tmp_path, "app.calc.missing") is None
    assert resolve_symbol_span(tmp_path, "app.calc") is None


def test_build_test_index_keys_functions_and_methods(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_api.py").write_text(
        "def test_health():\n    assert True\n\n\n"
        "class TestTasks:\n    def test_create(self):\n        assert True\n",
        encoding="utf-8",
    )

    index = build_test_index(tmp_path)

    assert index["test_api.test_health"] == Location(path="tests/test_api.py", start_line=1)
    assert index["test_api.TestTasks.test_create"] == Location(
        path="tests/test_api.py", start_line=6
    )


def test_related_tests_from_coverage_maps_span_to_tests():
    coverage_map = {
        "app/calc.py": {
            2: frozenset({"test_calc.test_add"}),
            3: frozenset({"test_calc.test_add", "test_calc.test_other"}),
        }
    }
    locations = {
        "test_calc.test_add": Location(path="tests/test_calc.py", start_line=4),
        "test_calc.test_other": Location(path="tests/test_calc.py", start_line=9),
    }
    span = SymbolSpan(path="app/calc.py", start_line=1, end_line=3)

    related = related_tests_from_coverage(coverage_map, span, locations.get, covers="app.calc.add")

    assert [test.test_id for test in related] == [
        "test_calc.test_add",
        "test_calc.test_other",
    ]
    assert all(test.covers == "app.calc.add" for test in related)
    assert all(test.provenance.source is Source.COVERAGE for test in related)
    assert all(test.provenance.confidence == 1.0 for test in related)
    assert all(test.provenance.authoritative for test in related)


def test_related_tests_from_coverage_returns_empty_when_span_uncovered():
    coverage_map = {"app/calc.py": {50: frozenset({"test_calc.test_add"})}}
    span = SymbolSpan(path="app/calc.py", start_line=1, end_line=3)

    related = related_tests_from_coverage(coverage_map, span, {}.get, covers="app.calc.add")

    assert related == []


def test_provider_returns_empty_for_unresolvable_target(tmp_path):
    _write_sample_repo(tmp_path)
    provider = CoverageProvider(tmp_path, coverage_map={})

    result = provider.repo_get_related_tests(RepoRef(_PROJECT, "deadbeef"), "app.calc.missing")

    assert result == []


def test_provider_uses_injected_coverage_map(tmp_path):
    _write_sample_repo(tmp_path)
    coverage_map = {"app/calc.py": {2: frozenset({"test_calc.test_add"})}}
    provider = CoverageProvider(tmp_path, coverage_map=coverage_map)

    result = provider.repo_get_related_tests(RepoRef(_PROJECT, "deadbeef"), "app.calc.add")

    assert [test.test_id for test in result] == ["test_calc.test_add"]
    assert result[0].location == Location(path="tests/test_calc.py", start_line=4)
    assert result[0].provenance.source is Source.COVERAGE


@requires_coverage
def test_provider_end_to_end_runs_coverage(tmp_path):
    _write_sample_repo(tmp_path)
    provider = CoverageProvider(tmp_path, venv_python=sys.executable)

    result = provider.repo_get_related_tests(RepoRef(_PROJECT, "deadbeef"), "app.calc.add")

    assert [test.test_id for test in result] == ["test_calc.test_add"]
    assert result[0].location.path == "tests/test_calc.py"
    assert result[0].provenance.source is Source.COVERAGE
    assert provider.repo_get_related_tests(RepoRef(_PROJECT, "deadbeef"), "app.calc.unused") == []
