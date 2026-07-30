"""Unit tests for the LSP-backed repository-intelligence provider."""

from __future__ import annotations

from services.repository_intelligence.normalization.models import RepoRef, Source, SymbolKind
from services.repository_intelligence.providers.lsp.lsp_client import LspServerSpec
from services.repository_intelligence.providers.lsp.lsp_provider import LspProvider

_SPEC = LspServerSpec(("terraform-ls", "serve"), "terraform")
_REPO = RepoRef(repo="infra", commit_sha="deadbeef")


class _FakeClient:
    def __init__(self, responses: dict[str, object]) -> None:
        self._responses = responses
        self.requests: list[tuple[str, dict]] = []

    def initialize(self, root) -> dict:
        return {}

    def request(self, method: str, params: dict):
        self.requests.append((method, params))
        return self._responses.get(method)


def _factory(client):
    def build(spec: LspServerSpec):
        return object(), client

    return build


class _RecordingClient:
    def __init__(self, symbols: list[dict]) -> None:
        self._symbols = symbols
        self.opened: list[str] = []

    def initialize(self, root) -> dict:
        return {}

    def notify(self, method: str, params: dict) -> None:
        if method == "textDocument/didOpen":
            self.opened.append(params["textDocument"]["uri"])

    def request(self, method: str, params: dict):
        if method == "workspace/symbol":
            query = params["query"]
            if not query:
                return self._symbols
            return [item for item in self._symbols if item["name"] == query]
        return None


def _location(root, line: int, end_line: int) -> dict:
    return {
        "uri": (root / "main.tf").resolve().as_uri(),
        "range": {
            "start": {"line": line, "character": 0},
            "end": {"line": end_line, "character": 1},
        },
    }


def test_find_symbol_normalizes_workspace_symbols(tmp_path):
    client = _FakeClient(
        {
            "workspace/symbol": [
                {
                    "name": "aws_instance.web",
                    "kind": 5,
                    "location": _location(tmp_path, 2, 4),
                    "containerName": "resource",
                }
            ]
        }
    )
    provider = LspProvider(tmp_path, _SPEC, commit="deadbeef", client_factory=_factory(client))

    symbols = provider.repo_find_symbol(_REPO, "aws_instance.web")

    assert len(symbols) == 1
    symbol = symbols[0]
    assert symbol.name == "aws_instance.web"
    assert symbol.kind is SymbolKind.CLASS
    assert symbol.location.path == "main.tf"
    assert symbol.location.start_line == 3
    assert symbol.location.end_line == 5
    assert symbol.language == "terraform"
    assert symbol.container == "resource"
    assert symbol.provenance.source is Source.LSP


def test_find_symbol_skips_non_file_uris(tmp_path):
    client = _FakeClient(
        {
            "workspace/symbol": [
                {"name": "ghost", "kind": 12, "location": {"uri": "untitled:ghost", "range": {}}}
            ]
        }
    )
    provider = LspProvider(tmp_path, _SPEC, client_factory=_factory(client))

    assert provider.repo_find_symbol(_REPO, "ghost") == []


def test_find_references_resolves_definition_then_references(tmp_path):
    definition = {
        "name": "aws_instance.web",
        "kind": 5,
        "location": _location(tmp_path, 2, 4),
    }
    client = _FakeClient(
        {
            "workspace/symbol": [definition],
            "textDocument/references": [
                _location(tmp_path, 10, 10),
                _location(tmp_path, 20, 20),
            ],
        }
    )
    provider = LspProvider(tmp_path, _SPEC, client_factory=_factory(client))

    references = provider.repo_find_references(_REPO, "aws_instance.web")

    assert [ref.location.start_line for ref in references] == [11, 21]
    assert all(ref.symbol == "web" for ref in references)
    assert all(not ref.is_definition for ref in references)
    assert all(ref.provenance.source is Source.LSP for ref in references)


def test_provider_is_unavailable_when_server_cannot_start(tmp_path):
    def failing(spec: LspServerSpec):
        raise OSError("terraform-ls not found")

    provider = LspProvider(tmp_path, _SPEC, client_factory=failing)

    assert provider.is_available() is False
    assert provider.repo_find_symbol(_REPO, "anything") == []


def test_index_status_reports_language_and_commit(tmp_path):
    provider = LspProvider(
        tmp_path, _SPEC, commit="deadbeef", client_factory=_factory(_FakeClient({}))
    )

    status = provider.repo_get_index_status(_REPO)

    assert status.is_fresh is True
    assert status.indexed_commit == "deadbeef"
    assert status.languages == ("terraform",)


def test_open_documents_sends_didopen_for_configured_suffixes(tmp_path):
    (tmp_path / "main.tf").write_text('resource "random_pet" "x" {}\n', encoding="utf-8")
    (tmp_path / "notes.md").write_text("ignore\n", encoding="utf-8")
    spec = LspServerSpec(("terraform-ls", "serve"), "terraform", (".tf",))
    location = _location(tmp_path, 0, 0)
    symbols = [{"name": 'resource "random_pet" "x"', "kind": 5, "location": location}]
    client = _RecordingClient(symbols)
    provider = LspProvider(tmp_path, spec, client_factory=_factory(client))

    result = provider.repo_find_symbol(_REPO, 'resource "random_pet" "x"')

    assert (tmp_path / "main.tf").resolve().as_uri() in client.opened
    assert all(not uri.endswith("notes.md") for uri in client.opened)
    assert len(result) == 1


def test_open_documents_skips_pruned_directories(tmp_path):
    (tmp_path / "main.tf").write_text("locals {}\n", encoding="utf-8")
    vendored = tmp_path / ".terraform" / "modules"
    vendored.mkdir(parents=True)
    (vendored / "dep.tf").write_text("locals {}\n", encoding="utf-8")
    spec = LspServerSpec(("terraform-ls", "serve"), "terraform", (".tf",))
    client = _RecordingClient([])
    provider = LspProvider(tmp_path, spec, client_factory=_factory(client))

    provider.repo_find_symbol(_REPO, "anything")

    assert client.opened == [(tmp_path / "main.tf").resolve().as_uri()]


_PHP_SPEC = LspServerSpec(("phpactor", "language-server"), "php", (".php",))


class _PhpClient:
    """Fake phpactor: substring-matches the short class name and keys references by uri."""

    def __init__(self, symbols: list[dict], refs_by_uri: dict[str, list[dict]]) -> None:
        self._symbols = symbols
        self._refs_by_uri = refs_by_uri
        self.symbol_queries: list[str] = []

    def initialize(self, root) -> dict:
        return {}

    def notify(self, method: str, params: dict) -> None:
        return None

    def request(self, method: str, params: dict):
        if method == "workspace/symbol":
            query = params["query"]
            self.symbol_queries.append(query)
            if not query:
                return self._symbols
            return [s for s in self._symbols if query in s["name"].rsplit("\\", 1)[-1]]
        if method == "textDocument/references":
            return self._refs_by_uri.get(params["textDocument"]["uri"], [])
        return None


def _php_location(tmp_path, filename: str, line: int) -> dict:
    return {
        "uri": (tmp_path / "src" / filename).resolve().as_uri(),
        "range": {
            "start": {"line": line, "character": 6},
            "end": {"line": line, "character": 20},
        },
    }


def test_find_symbol_strips_php_namespace_separator(tmp_path):
    (tmp_path / "src").mkdir()
    symbols = [
        {"name": "App\\Task", "kind": 5, "location": _php_location(tmp_path, "Task.php", 5)},
        {
            "name": "App\\TaskStore",
            "kind": 5,
            "location": _php_location(tmp_path, "TaskStore.php", 5),
        },
    ]
    client = _PhpClient(symbols, {})
    provider = LspProvider(tmp_path, _PHP_SPEC, client_factory=_factory(client))

    result = provider.repo_find_symbol(_REPO, "App\\Task")

    assert client.symbol_queries[-1] == "Task"
    assert "App\\Task" in {symbol.name for symbol in result}


def test_find_references_resolves_namespaced_definition_by_name(tmp_path):
    (tmp_path / "src").mkdir()
    task_uri = _php_location(tmp_path, "Task.php", 5)["uri"]
    store_uri = _php_location(tmp_path, "TaskStore.php", 5)["uri"]
    symbols = [
        {
            "name": "App\\TaskStore",
            "kind": 5,
            "location": _php_location(tmp_path, "TaskStore.php", 5),
        },
        {"name": "App\\Task", "kind": 5, "location": _php_location(tmp_path, "Task.php", 5)},
    ]
    refs_by_uri = {
        task_uri: [_php_location(tmp_path, "TaskService.php", 11)],
        store_uri: [_php_location(tmp_path, "TaskService.php", 7)],
    }
    client = _PhpClient(symbols, refs_by_uri)
    provider = LspProvider(tmp_path, _PHP_SPEC, client_factory=_factory(client))

    references = provider.repo_find_references(_REPO, "App\\Task", include_definition=False)

    assert [ref.location.start_line for ref in references] == [12]
    assert all(ref.symbol == "Task" for ref in references)
