"""Unit tests for the ast-grep-backed structural provider."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from services.repository_intelligence.normalization.models import (
    NON_AUTHORITATIVE_SOURCES,
    RepoRef,
    Source,
)
from services.repository_intelligence.providers.ast_grep.ast_grep_provider import (
    AstGrepProvider,
    hit_from_match,
    matches_from_json,
)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ast_grep"
_PROJECT = "agentic-demo-service"

_ast_grep_available = shutil.which("ast-grep") is not None
requires_ast_grep = pytest.mark.skipif(not _ast_grep_available, reason="ast-grep must be on PATH")


def _read_fixture(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


def test_matches_from_json_parses_routes_in_deterministic_order():
    matches = matches_from_json(_read_fixture("routes.json"))

    assert [m.location.path for m in matches] == ["app/main.py"] * 4
    assert [m.location.start_line for m in matches] == [17, 23, 29, 35]
    assert [m.single["METHOD"] for m in matches] == ["get", "get", "post", "get"]


def test_matches_from_json_converts_lines_to_one_based_and_keeps_columns():
    first = matches_from_json(_read_fixture("routes.json"))[0]

    assert first.location.start_line == 17
    assert first.location.start_col == 0
    assert first.location.end_line == 17
    assert first.multi["ARGS"] == ('"/health"',)


def test_matches_from_json_extracts_model_names():
    matches = matches_from_json(_read_fixture("models.json"))

    assert [(m.location.path, m.location.start_line) for m in matches] == [
        ("app/models.py", 16),
        ("app/models.py", 24),
    ]
    assert [m.single["NAME"] for m in matches] == ["TaskCreate", "Task"]


def test_matches_from_json_parses_raise_site_arguments():
    matches = matches_from_json(_read_fixture("raises.json"))

    assert len(matches) == 1
    match = matches[0]
    assert match.location.path == "app/main.py"
    assert match.location.start_line == 40
    assert match.location.start_col == 8
    assert match.single == {}
    assert "status_code=404" in match.multi["ARGS"]


def test_matches_from_json_handles_empty_output():
    assert matches_from_json("") == []


def test_hit_from_match_carries_authoritative_ast_grep_provenance():
    match = matches_from_json(_read_fixture("routes.json"))[0]

    hit = hit_from_match(match)

    assert hit.snippet == '@app.get("/health")'
    assert hit.score == 1.0
    assert hit.location.start_line == 17
    assert hit.provenance.source is Source.AST_GREP
    assert hit.provenance.confidence == 1.0
    assert hit.provenance.source not in NON_AUTHORITATIVE_SOURCES
    assert hit.provenance.authoritative


def _init_corpus(repo_root: Path) -> None:
    app = repo_root / "app"
    app.mkdir()
    (app / "main.py").write_text(
        "from fastapi import FastAPI, HTTPException\n\n"
        "app = FastAPI()\n\n"
        '@app.get("/health")\n'
        "def health():\n"
        '    return {"status": "ok"}\n\n'
        '@app.post("/tasks", status_code=201)\n'
        "def create_task():\n"
        '    raise HTTPException(status_code=404, detail="nope")\n',
        encoding="utf-8",
    )
    (app / "models.py").write_text(
        "from pydantic import BaseModel\n\nclass Task(BaseModel):\n    id: int\n    title: str\n",
        encoding="utf-8",
    )


@requires_ast_grep
def test_find_routes_against_a_real_corpus(tmp_path: Path):
    repo_root = tmp_path / _PROJECT
    repo_root.mkdir()
    _init_corpus(repo_root)
    provider = AstGrepProvider(repo_root)

    routes = provider.find_routes()

    assert {m.single["METHOD"] for m in routes} == {"get", "post"}
    assert all(m.location.path == "app/main.py" for m in routes)


@requires_ast_grep
def test_find_models_and_raise_sites_against_a_real_corpus(tmp_path: Path):
    repo_root = tmp_path / _PROJECT
    repo_root.mkdir()
    _init_corpus(repo_root)
    provider = AstGrepProvider(repo_root)

    models = provider.find_models()
    raises = provider.find_raise_sites()

    assert [m.single["NAME"] for m in models] == ["Task"]
    assert len(raises) == 1
    assert raises[0].location.path == "app/main.py"


@requires_ast_grep
def test_repo_search_structural_returns_normalized_hits(tmp_path: Path):
    repo_root = tmp_path / _PROJECT
    repo_root.mkdir()
    _init_corpus(repo_root)
    provider = AstGrepProvider(repo_root)
    repo = RepoRef(repo=_PROJECT, commit_sha="head")

    hits = provider.repo_search_structural(repo, "@app.$METHOD($$$ARGS)")

    assert hits
    assert all(h.provenance.source is Source.AST_GREP for h in hits)
    assert all(h.location.path == "app/main.py" for h in hits)
