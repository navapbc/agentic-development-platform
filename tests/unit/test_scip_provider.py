"""Unit tests for the SCIP-backed repository-intelligence provider (P2.1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from services.repository_intelligence.indexing.scip_index import ScipIndex
from services.repository_intelligence.normalization.models import (
    NON_AUTHORITATIVE_SOURCES,
    RepoRef,
    Source,
    SymbolKind,
)
from services.repository_intelligence.providers.scip.scip_provider import ScipProvider

_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "scip"
    / "agentic-demo-service@0744a6e.scip.json"
)
_PROJECT = "agentic-demo-service"
_COMMIT = "0744a6e16964f33827d00bef41a1e8071a70449b"


@pytest.fixture
def provider(tmp_path: Path) -> ScipProvider:
    data = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    index = ScipIndex.from_scip_json(data, _PROJECT)
    return ScipProvider(tmp_path, index=index, commit=_COMMIT)


@pytest.fixture
def repo() -> RepoRef:
    return RepoRef(repo=_PROJECT, commit_sha=_COMMIT)


def test_find_symbol_resolves_function_definition(provider: ScipProvider, repo: RepoRef):
    symbols = provider.repo_find_symbol(repo, "create_task")
    assert len(symbols) == 1
    symbol = symbols[0]
    assert symbol.name == "app.main.create_task"
    assert symbol.kind is SymbolKind.FUNCTION
    assert symbol.location.path == "app/main.py"
    assert symbol.provenance.source is Source.SCIP


def test_find_symbol_maps_class_kind(provider: ScipProvider, repo: RepoRef):
    symbols = provider.repo_find_symbol(repo, "Task")
    assert [s.name for s in symbols] == ["app.models.Task"]
    assert symbols[0].kind is SymbolKind.CLASS


def test_provenance_is_authoritative(provider: ScipProvider, repo: RepoRef):
    symbol = provider.repo_find_symbol(repo, "create_task")[0]
    assert symbol.provenance.source not in NON_AUTHORITATIVE_SOURCES
    assert symbol.provenance.authoritative


def test_references_exclude_definition_when_requested(provider: ScipProvider, repo: RepoRef):
    refs = provider.repo_find_references(repo, "app.models.Task", include_definition=False)
    assert refs
    assert all(not ref.is_definition for ref in refs)
    assert all(ref.provenance.source is Source.SCIP for ref in refs)


def test_references_include_single_definition(provider: ScipProvider, repo: RepoRef):
    refs = provider.repo_find_references(repo, "app.models.Task", include_definition=True)
    definitions = [ref for ref in refs if ref.is_definition]
    assert len(definitions) == 1
    assert definitions[0].location.path == "app/models.py"


def test_implementations_stdlib_base(provider: ScipProvider, repo: RepoRef):
    impls = provider.repo_find_implementations(repo, "StrEnum")
    assert {s.name for s in impls} == {"app.models.Status"}


def test_implementations_third_party_base(provider: ScipProvider, repo: RepoRef):
    impls = provider.repo_find_implementations(repo, "BaseModel")
    assert {s.name for s in impls} == {"app.models.Task", "app.models.TaskCreate"}
    assert all(s.provenance.source is Source.SCIP for s in impls)


def test_module_dependencies_strip_project_prefix_and_flag_external(
    provider: ScipProvider, repo: RepoRef
):
    edges = provider.repo_get_module_dependencies(repo, f"{_PROJECT}.tests.test_api")
    by_target = {edge.to_module: edge for edge in edges}
    assert "app.main" in by_target
    assert by_target["app.main"].kind == "import"
    assert "pytest" in by_target
    assert by_target["pytest"].kind == "external_import"


def test_callers_carry_scip_provenance(provider: ScipProvider, repo: RepoRef):
    edges = provider.repo_get_callers(repo, "app.store.TaskStore.add")
    assert edges
    for edge in edges:
        assert edge.provenance.source is Source.SCIP
        assert edge.callee.name == "app.store.TaskStore.add"
        assert edge.call_site is not None


def test_related_tests_defers_to_coverage(provider: ScipProvider, repo: RepoRef):
    assert provider.repo_get_related_tests(repo, "app.main.create_task") == []


def test_index_status_reports_indexed_commit(provider: ScipProvider, repo: RepoRef):
    status = provider.repo_get_index_status(repo)
    assert status.indexed_commit == _COMMIT
    assert status.languages == ("python",)
    assert status.detail is not None
