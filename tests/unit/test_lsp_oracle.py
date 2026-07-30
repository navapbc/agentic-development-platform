"""Unit tests for the LSP-backed gold oracle."""

from __future__ import annotations

from services.repository_intelligence.eval import lsp_oracle
from services.repository_intelligence.eval.gold_schema import QueryType
from services.repository_intelligence.providers.lsp.lsp_client import LspError, LspServerSpec

_SPEC = LspServerSpec(("terraform-ls", "serve"), "terraform", (".tf",))
_PHP_SPEC = LspServerSpec(("phpactor", "language-server"), "php", (".php",))


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


def _phar_symbol(name: str) -> dict:
    return {
        "name": name,
        "kind": 12,
        "location": {
            "uri": "phar:///opt/phpactor.phar/vendor/stubs/standard.php",
            "range": {
                "start": {"line": 0, "character": 0},
                "end": {"line": 0, "character": 3},
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


class _PhpClient:
    def __init__(
        self,
        symbols: list[dict],
        refs_by_line: dict[int, list[dict]],
        raise_lines: frozenset[int] = frozenset(),
    ) -> None:
        self._symbols = symbols
        self._refs_by_line = refs_by_line
        self._raise_lines = raise_lines

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
            line = params["position"]["line"]
            if line in self._raise_lines:
                raise LspError("UnknownDocument")
            return self._refs_by_line.get(line, [])
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


def test_collect_lsp_records_excludes_out_of_repo_stub_symbols(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "Task.php").write_text("<?php\n", encoding="utf-8")
    symbols = [
        _symbol("App\\Task", tmp_path, "src/Task.php", 4),
        _phar_symbol("strlen"),
        _phar_symbol("stdClass"),
    ]
    client = _PhpClient(symbols, {4: [_reference(tmp_path, "src/TaskStore.php", 10)]})
    monkeypatch.setattr(lsp_oracle, "spawn", lambda spec: (_FakeProcess(), client))

    records = lsp_oracle.collect_lsp_records(
        tmp_path, project="php", commit="c0ffee", spec=_PHP_SPEC, oracle="phpactor"
    )

    definitions = [r for r in records if r.query_type is QueryType.SYMBOL_DEFINITION]
    references = [r for r in records if r.query_type is QueryType.REFERENCES]
    assert {r.input["name"] for r in definitions} == {"App\\Task"}
    assert len(references) == 1
    assert references[0].input["symbol"] == "App\\Task"


def test_collect_lsp_records_skips_symbol_when_references_raise(tmp_path, monkeypatch):
    symbols = [
        _symbol("App\\Task", tmp_path, "src/Task.php", 0),
        _symbol("App\\TaskStore", tmp_path, "src/TaskStore.php", 5),
    ]
    client = _PhpClient(
        symbols,
        {5: [_reference(tmp_path, "src/TaskService.php", 9)]},
        raise_lines=frozenset({0}),
    )
    monkeypatch.setattr(lsp_oracle, "spawn", lambda spec: (_FakeProcess(), client))

    records = lsp_oracle.collect_lsp_records(
        tmp_path, project="php", commit="c0ffee", spec=_PHP_SPEC, oracle="phpactor"
    )

    definitions = [r for r in records if r.query_type is QueryType.SYMBOL_DEFINITION]
    references = [r for r in records if r.query_type is QueryType.REFERENCES]
    assert {r.input["name"] for r in definitions} == {"App\\Task", "App\\TaskStore"}
    assert [r.input["symbol"] for r in references] == ["App\\TaskStore"]
