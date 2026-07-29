"""Unit tests for the Zoekt-backed repository-intelligence provider."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from services.repository_intelligence.normalization.models import (
    NON_AUTHORITATIVE_SOURCES,
    RepoRef,
    Source,
)
from services.repository_intelligence.providers.zoekt.zoekt_provider import (
    ZoektProvider,
    hits_from_jsonl,
)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "zoekt"
_PROJECT = "agentic-demo-service"

_zoekt_available = shutil.which("zoekt") is not None and shutil.which("zoekt-index") is not None
requires_zoekt = pytest.mark.skipif(
    not _zoekt_available, reason="zoekt and zoekt-index must be on PATH"
)


def _read_fixture(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


def test_hits_from_jsonl_orders_by_descending_score():
    hits = hits_from_jsonl(_read_fixture("search-taskstore.jsonl"), limit=50)

    assert [(h.location.path, h.location.start_line, h.snippet) for h in hits] == [
        ("app/store.py", 15, "class TaskStore:"),
        ("app/store.py", 42, "store = TaskStore()"),
    ]
    assert [h.score for h in hits] == [501.0, 500.5]
    assert [h.location.start_col for h in hits] == [6, 8]


def test_hits_from_jsonl_breaks_score_ties_by_path_then_line():
    hits = hits_from_jsonl(_read_fixture("search-create_task.jsonl"), limit=50)

    assert [h.location.path for h in hits] == ["app/main.py", "tests/test_api.py"]
    assert all(h.score == 501.0 for h in hits)
    assert hits[0].snippet == "def create_task(data: TaskCreate) -> Task:"
    assert hits[1].snippet == "def test_create_task_defaults_status_to_todo(client):"


def test_hits_from_jsonl_respects_limit():
    hits = hits_from_jsonl(_read_fixture("search-taskstore.jsonl"), limit=1)

    assert len(hits) == 1
    assert hits[0].location.start_line == 15


def test_hits_from_jsonl_handles_empty_output():
    assert hits_from_jsonl("", limit=50) == []


def test_hits_carry_authoritative_zoekt_provenance():
    hit = hits_from_jsonl(_read_fixture("search-taskstore.jsonl"), limit=1)[0]

    assert hit.provenance.source is Source.ZOEKT
    assert hit.provenance.confidence == 1.0
    assert hit.provenance.source not in NON_AUTHORITATIVE_SOURCES
    assert hit.provenance.authoritative


def _init_corpus(repo_root: Path) -> str:
    (repo_root / "app").mkdir()
    (repo_root / "app" / "store.py").write_text(
        "class TaskStore:\n    def __init__(self):\n        self.tasks = {}\n",
        encoding="utf-8",
    )
    (repo_root / "app" / "main.py").write_text(
        "from app.store import TaskStore\n\nstore = TaskStore()\n",
        encoding="utf-8",
    )
    env = [
        "-c",
        "user.email=test@example.com",
        "-c",
        "user.name=test",
    ]
    subprocess.run(["git", "init", "-q"], cwd=repo_root, check=True)
    subprocess.run(["git", *env, "add", "."], cwd=repo_root, check=True)
    subprocess.run(["git", *env, "commit", "-q", "-m", "init"], cwd=repo_root, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return head.stdout.strip()


@requires_zoekt
def test_build_and_search_roundtrip(tmp_path: Path):
    repo_root = tmp_path / _PROJECT
    repo_root.mkdir()
    _init_corpus(repo_root)
    provider = ZoektProvider(repo_root, index_root=tmp_path / "index")
    repo = RepoRef(repo=_PROJECT, commit_sha="head")

    hits = provider.repo_search_code(repo, "TaskStore")

    assert hits
    assert all(h.provenance.source is Source.ZOEKT for h in hits)
    assert {h.location.path for h in hits} <= {"app/store.py", "app/main.py"}


@requires_zoekt
def test_index_status_reports_head_commit(tmp_path: Path):
    repo_root = tmp_path / _PROJECT
    repo_root.mkdir()
    head = _init_corpus(repo_root)
    provider = ZoektProvider(repo_root, index_root=tmp_path / "index")
    repo = RepoRef(repo=_PROJECT, commit_sha=head)

    status = provider.repo_get_index_status(repo)

    assert status.indexed_commit == head
    assert status.is_fresh
