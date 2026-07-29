"""Unit tests for the codebase-memory-mcp provider (candidate + staleness roles)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from services.repository_intelligence.normalization.models import (
    CallEdge,
    Location,
    Provenance,
    Reference,
    RepoRef,
    Source,
    Symbol,
    SymbolKind,
)
from services.repository_intelligence.providers.cbm.cbm_provider import (
    CBMError,
    CBMProvider,
)

_PROJECT = "agentic-demo-service"
_REPO = RepoRef(_PROJECT, "deadbeef")

_CBM_BINARY = os.environ.get("CBM_BINARY", "")
_cbm_runnable = bool(_CBM_BINARY) and Path(_CBM_BINARY).exists()
requires_cbm = pytest.mark.skipif(
    not _cbm_runnable, reason="CBM_BINARY must point to a runnable binary"
)


def _provider(tmp_path: Path, binary: str | Path = "cbm-does-not-exist") -> CBMProvider:
    provider = CBMProvider(tmp_path, binary, allowed_root=tmp_path)
    provider._project = _PROJECT
    return provider


def _symbol(name: str, path: str, line: int) -> Symbol:
    return Symbol(
        name=name,
        kind=SymbolKind.FUNCTION,
        location=Location(path=path, start_line=line),
        provenance=Provenance(source=Source.CBM, confidence=0.8),
        language="python",
    )


def test_rejects_repo_outside_allowed_root(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(CBMError):
        CBMProvider(outside, "cbm", allowed_root=allowed)


def test_is_available_false_when_binary_missing(tmp_path):
    provider = _provider(tmp_path, binary="definitely-not-a-real-binary")

    assert provider.is_available() is False


def test_is_available_true_for_runnable_stub(tmp_path):
    stub = tmp_path / "cbm-stub"
    stub.write_text("#!/bin/sh\necho 'codebase-memory-mcp dev'\n", encoding="utf-8")
    stub.chmod(0o755)
    provider = CBMProvider(tmp_path, stub, allowed_root=tmp_path)

    assert provider.is_available() is True


def test_candidate_references_dedupes_to_file_level(tmp_path, monkeypatch):
    provider = _provider(tmp_path)
    refs = [
        Reference(
            symbol="add",
            location=Location(path="app/store.py", start_line=40),
            is_definition=False,
            provenance=Provenance(source=Source.CBM, confidence=0.7),
        ),
        Reference(
            symbol="add",
            location=Location(path="app/store.py", start_line=12),
            is_definition=False,
            provenance=Provenance(source=Source.CBM, confidence=0.7),
        ),
        Reference(
            symbol="add",
            location=Location(path="app/main.py", start_line=30),
            is_definition=False,
            provenance=Provenance(source=Source.CBM, confidence=0.7),
        ),
    ]
    monkeypatch.setattr(provider, "repo_find_references", lambda *a, **k: refs)

    candidates = provider.candidate_references(_REPO, "add")

    assert [(c.location.path, c.location.start_line) for c in candidates] == [
        ("app/main.py", 30),
        ("app/store.py", 12),
    ]
    assert all(not c.is_definition for c in candidates)
    assert all(c.provenance.source is Source.CBM for c in candidates)
    assert all(c.provenance.confidence == 0.5 for c in candidates)


def test_candidate_callers_dedupes_to_file_level(tmp_path, monkeypatch):
    provider = _provider(tmp_path)
    callee = _symbol("app.store.TaskStore.add", "app/store.py", 29)
    edges = [
        CallEdge(
            caller=_symbol("app.main.create_task", "app/main.py", 30),
            callee=callee,
            provenance=Provenance(source=Source.CBM, confidence=0.8),
        ),
        CallEdge(
            caller=_symbol("app.main.helper", "app/main.py", 12),
            callee=callee,
            provenance=Provenance(source=Source.CBM, confidence=0.8),
        ),
    ]
    monkeypatch.setattr(provider, "repo_get_callers", lambda *a, **k: edges)

    candidates = provider.candidate_callers(_REPO, "add")

    assert [c.caller.location.start_line for c in candidates] == [12]
    assert candidates[0].caller.location.path == "app/main.py"
    assert candidates[0].provenance.source is Source.CBM
    assert candidates[0].provenance.confidence == 0.5
    assert candidates[0].call_site == Location(path="app/main.py", start_line=12)


@requires_cbm
def test_index_reports_staleness_against_real_binary(tmp_path):
    corpus = Path("~/Documents/Projects/Nava/agentic-demo-service").expanduser()
    provider = CBMProvider(corpus, _CBM_BINARY, allowed_root=corpus, cache_dir=tmp_path)

    assert provider.is_available() is True
    provider.index()
    status = provider.repo_get_index_status(_REPO)

    assert status.indexed_commit is not None
    assert status.is_fresh is True


@requires_cbm
def test_candidate_references_are_file_level_against_real_binary(tmp_path):
    corpus = Path("~/Documents/Projects/Nava/agentic-demo-service").expanduser()
    provider = CBMProvider(corpus, _CBM_BINARY, allowed_root=corpus, cache_dir=tmp_path)
    provider.index()

    candidates = provider.candidate_references(_REPO, "TaskStore")

    paths = [c.location.path for c in candidates]
    assert paths == sorted(set(paths))
    assert all(c.provenance.confidence == 0.5 for c in candidates)
