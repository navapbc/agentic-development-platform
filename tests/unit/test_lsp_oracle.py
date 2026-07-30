"""Unit tests for the LSP-backed gold oracle."""

from __future__ import annotations

from services.repository_intelligence.eval import lsp_oracle
from services.repository_intelligence.eval.gold_schema import QueryType
from services.repository_intelligence.providers.lsp.lsp_client import LspServerSpec

_SPEC = LspServerSpec(("terraform-ls", "serve"), "terraform", (".tf",))


def _symbol(name: str, root, file: str, line: int) -> dict:
    return {
        "name": name,
        "kind": 5,
        "location": {
            "uri": (root / file).resolve().as_uri(),
            "range": {
                "start": {"line": line, "character": 0},
                "end": {"line": line, "character": 5},
            },
        },
    }


def _reference(root, file: str, line: int) -> dict:
    return {
        "uri": (root / file).resolve().as_uri(),
        "range": {
            "start": {"line": line, "character": 2},
            "end": {"line": line, "character": 8},
        },
    }


class _FakeProcess:
    def kill(self) -> None:
        return None


class _OracleClient:
    def __init__(self, symbols: list[dict], refs_by_line: dict[int, list[dict]]) -> None:
        self._symbols = symbols
        self._refs_by_line = refs_by_line

    def initialize(self, root) -> dict:
        return {}

    def notify(self, method: str, params: dict) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def request(self, method: str, params: dict):
        if method == "workspace/symbol":
            query = params["query"]
            if not query:
                return self._symbols
            return [item for item in self._symbols if item["name"] == query]
        if method == "textDocument/references":
            return self._refs_by_line.get(params["position"]["line"], [])
        return None


def test_collect_lsp_records_builds_definitions_and_references(tmp_path, monkeypatch):
    (tmp_path / "main.tf").write_text("locals {}\n", encoding="utf-8")
    symbols = [
        _symbol('variable "environment"', tmp_path, "variables.tf", 0),
        _symbol('resource "random_pet" "task_id"', tmp_path, "main.tf", 5),
    ]
    refs_by_line = {
        0: [_reference(tmp_path, "main.tf", 12), _reference(tmp_path, "main.tf", 14)],
        5: [],
    }
    client = _OracleClient(symbols, refs_by_line)
    monkeypatch.setattr(lsp_oracle, "spawn", lambda spec: (_FakeProcess(), client))

    records = lsp_oracle.collect_lsp_records(
        tmp_path, project="infra", commit="c0ffee", spec=_SPEC, oracle="terraform-ls"
    )

    definitions = [r for r in records if r.query_type is QueryType.SYMBOL_DEFINITION]
    references = [r for r in records if r.query_type is QueryType.REFERENCES]

    assert {r.input["name"] for r in definitions} == {
        'variable "environment"',
        'resource "random_pet" "task_id"',
    }
    variable_def = next(r for r in definitions if r.input["name"] == 'variable "environment"')
    assert variable_def.expected[0].path == "variables.tf"
    assert variable_def.expected[0].line == 1
    assert variable_def.oracle == "terraform-ls"

    assert len(references) == 1
    assert references[0].input["symbol"] == 'variable "environment"'
    assert [item.line for item in references[0].expected] == [13, 15]


def test_collect_lsp_records_groups_duplicate_headers(tmp_path, monkeypatch):
    symbols = [
        _symbol('output "queue_arn"', tmp_path, "outputs.tf", 5),
        _symbol('output "queue_arn"', tmp_path, "modules/task_queue/outputs.tf", 5),
    ]
    client = _OracleClient(symbols, {5: []})
    monkeypatch.setattr(lsp_oracle, "spawn", lambda spec: (_FakeProcess(), client))

    records = lsp_oracle.collect_lsp_records(
        tmp_path, project="infra", commit="c0ffee", spec=_SPEC, oracle="terraform-ls"
    )

    definitions = [r for r in records if r.query_type is QueryType.SYMBOL_DEFINITION]
    assert len(definitions) == 1
    paths = {item.path for item in definitions[0].expected}
    assert paths == {"outputs.tf", "modules/task_queue/outputs.tf"}
