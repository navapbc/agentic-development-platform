"""Unit tests for the language-aware gold-set builder."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from services.repository_intelligence.eval import build_gold_set
from services.repository_intelligence.eval.gold_schema import read_gold_set
from services.repository_intelligence.indexing.scip_builder import (
    SCIP_PYTHON_INDEXER,
    SCIP_TYPESCRIPT_INDEXER,
)

_PROJECT = "proj"


def _ts_scip_json() -> dict:
    def sym(descriptor: str) -> str:
        return f"scip-typescript npm {_PROJECT} 0.1.0 {descriptor}"

    def occ(descriptor: str, rng: list[int], role: int) -> dict:
        return {"symbol": sym(descriptor), "range": rng, "symbol_roles": role}

    error_base = "scip-typescript npm typescript 5.9.3 lib/`lib.es5.d.ts`/Error#"
    return {
        "documents": [
            {
                "relative_path": "src/models.ts",
                "symbols": [
                    {"symbol": sym("src/`models.ts`/Status#")},
                    {
                        "symbol": sym("src/`store.ts`/DuplicateTaskError#"),
                        "relationships": [{"symbol": error_base, "is_implementation": True}],
                    },
                ],
                "occurrences": [
                    occ("src/`models.ts`/Status#", [0, 13, 0, 19], 1),
                    occ("src/`store.ts`/DuplicateTaskError#", [3, 13, 3, 31], 1),
                    occ("src/`models.ts`/Status#", [8, 10, 8, 16], 0),
                ],
            }
        ]
    }


def _write_json(tmp_path: Path, payload: dict) -> Path:
    scip_json = tmp_path / "index.scip.json"
    scip_json.write_text(json.dumps(payload), encoding="utf-8")
    return scip_json


def test_indexer_version_reads_the_pinned_command():
    assert build_gold_set._indexer_version(SCIP_TYPESCRIPT_INDEXER) == "0.3.14"
    assert build_gold_set._indexer_version(SCIP_PYTHON_INDEXER) == "0.6.6"


def test_main_typescript_emits_structural_records_only(tmp_path, monkeypatch):
    repo_root = tmp_path / _PROJECT
    repo_root.mkdir()
    scip_json = _write_json(tmp_path, _ts_scip_json())
    out_path = tmp_path / "gold.jsonl"
    monkeypatch.setattr(build_gold_set, "_resolve_commit", lambda _root: "a" * 40)

    exit_code = build_gold_set.main(
        [
            "--repo-root",
            str(repo_root),
            "--language",
            "typescript",
            "--scip-json",
            str(scip_json),
            "--out",
            str(out_path),
            "--no-coverage",
        ]
    )

    assert exit_code == 0
    meta, records = read_gold_set(out_path)
    assert meta is not None
    assert meta.oracles["structure"] == "scip-typescript@0.3.14"
    query_types = {str(record.query_type) for record in records}
    assert "module_dependencies" not in query_types
    assert "related_tests" not in query_types
    assert {"symbol_definition", "references", "implementations"} <= query_types
    assert all(
        record.oracle == "scip-typescript"
        for record in records
        if str(record.query_type) != "related_tests"
    )


def test_main_errors_for_a_language_without_an_indexer(tmp_path, monkeypatch):
    repo_root = tmp_path / _PROJECT
    repo_root.mkdir()
    scip_json = _write_json(tmp_path, {"documents": []})
    monkeypatch.setattr(build_gold_set, "_resolve_commit", lambda _root: "a" * 40)

    with pytest.raises(SystemExit):
        build_gold_set.main(
            [
                "--repo-root",
                str(repo_root),
                "--language",
                "ruby",
                "--scip-json",
                str(scip_json),
                "--out",
                str(tmp_path / "gold.jsonl"),
                "--no-coverage",
            ]
        )
