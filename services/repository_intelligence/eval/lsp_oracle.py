"""LSP-backed gold oracle for languages without a SCIP indexer.

Drives a language server directly to label symbol definitions and references, so
the harness can score the runtime LspProvider against an independently collected
ground truth. Coordinate conversion here is deliberately separate from the
provider so a normalization regression in either surface is caught.
"""

from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import unquote, urlparse

from services.repository_intelligence.eval.gold_schema import (
    ExpectedItem,
    GoldRecord,
    QueryType,
)
from services.repository_intelligence.providers.lsp.lsp_client import (
    LspClient,
    LspError,
    LspServerSpec,
    spawn,
)

_Location = tuple[str, int, int]


def _relative(uri: object, root: Path) -> str | None:
    if not isinstance(uri, str) or not uri:
        return None
    parsed = urlparse(uri)
    if parsed.scheme and parsed.scheme != "file":
        return None
    path = Path(unquote(parsed.path))
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _location(raw: object, root: Path) -> _Location | None:
    if not isinstance(raw, dict):
        return None
    rel = _relative(raw.get("uri"), root)
    if rel is None:
        return None
    start = (raw.get("range") or {}).get("start") or {}
    return rel, int(start.get("line", 0)) + 1, int(start.get("character", 0))


def _open_documents(client: LspClient, root: Path, spec: LspServerSpec) -> None:
    opened = 0
    for suffix in spec.source_suffixes:
        for path in sorted(root.rglob(f"*{suffix}")):
            if not path.is_file() or {".git", ".terraform"}.intersection(path.parts):
                continue
            client.notify(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": path.resolve().as_uri(),
                        "languageId": spec.language_id,
                        "version": 1,
                        "text": path.read_text(encoding="utf-8"),
                    }
                },
            )
            opened += 1
    if opened:
        _await_symbols(client)


def _await_symbols(client: LspClient) -> None:
    previous = -1
    for _ in range(40):
        try:
            raw = client.request("workspace/symbol", {"query": ""})
        except LspError:
            return
        count = len(raw) if isinstance(raw, list) else 0
        if count and count == previous:
            return
        previous = count
        time.sleep(0.05)


def _all_symbols(client: LspClient) -> list[dict]:
    raw = client.request("workspace/symbol", {"query": ""})
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _definition_location(client: LspClient, name: str) -> dict | None:
    raw = client.request("workspace/symbol", {"query": name})
    if not isinstance(raw, list):
        return None
    for item in raw:
        if isinstance(item, dict) and item.get("name") == name:
            location = item.get("location")
            if isinstance(location, dict) and "uri" in location and "range" in location:
                return location
    return None


def _symbol_definition_records(
    symbols: list[dict], root: Path, project: str, commit: str, oracle: str
) -> list[GoldRecord]:
    by_name: dict[str, list[_Location]] = {}
    for item in symbols:
        name = item.get("name")
        location = _location(item.get("location"), root)
        if isinstance(name, str) and name and location is not None:
            by_name.setdefault(name, []).append(location)
    records: list[GoldRecord] = []
    for name, locations in sorted(by_name.items()):
        expected = tuple(
            ExpectedItem(path=path, line=line, col=col, symbol=name)
            for path, line, col in sorted(locations)
        )
        records.append(
            GoldRecord(
                repo=project,
                commit=commit,
                query_type=QueryType.SYMBOL_DEFINITION,
                input={"name": name},
                expected=expected,
                oracle=oracle,
            )
        )
    return records


def _references_records(
    client: LspClient, symbols: list[dict], root: Path, project: str, commit: str, oracle: str
) -> list[GoldRecord]:
    records: list[GoldRecord] = []
    for name in sorted({item.get("name") for item in symbols if isinstance(item.get("name"), str)}):
        location = _definition_location(client, name)
        if location is None:
            continue
        raw = client.request(
            "textDocument/references",
            {
                "textDocument": {"uri": location["uri"]},
                "position": location["range"]["start"],
                "context": {"includeDeclaration": False},
            },
        )
        items = raw if isinstance(raw, list) else []
        parsed = sorted({loc for item in items if (loc := _location(item, root)) is not None})
        if not parsed:
            continue
        expected = tuple(ExpectedItem(path=path, line=line, col=col) for path, line, col in parsed)
        records.append(
            GoldRecord(
                repo=project,
                commit=commit,
                query_type=QueryType.REFERENCES,
                input={"symbol": name, "name": name},
                expected=expected,
                oracle=oracle,
            )
        )
    return records


def collect_lsp_records(
    repo_root: Path, *, project: str, commit: str, spec: LspServerSpec, oracle: str
) -> list[GoldRecord]:
    """Return symbol-definition and reference gold records from a language server."""
    root = Path(repo_root).resolve()
    process, client = spawn(spec)
    try:
        client.initialize(root)
        _open_documents(client, root, spec)
        symbols = _all_symbols(client)
        records = _symbol_definition_records(symbols, root, project, commit, oracle)
        records += _references_records(client, symbols, root, project, commit, oracle)
        return records
    finally:
        client.shutdown()
        process.kill()
