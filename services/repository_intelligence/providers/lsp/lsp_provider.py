"""Repository-intelligence provider backed by a Language Server (LSP fallback tier)."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from services.repository_intelligence.normalization.models import (
    IndexStatus,
    Location,
    Provenance,
    Reference,
    RepoRef,
    Source,
    Symbol,
    SymbolKind,
)
from services.repository_intelligence.providers.lsp.lsp_client import (
    LspClient,
    LspError,
    LspServerSpec,
    spawn,
)

_LSP_CONFIDENCE = 0.9


def _short_name(name: str) -> str:
    return name.rsplit(".", 1)[-1].rsplit("\\", 1)[-1]


_LSP_SYMBOL_KIND: dict[int, SymbolKind] = {
    2: SymbolKind.MODULE,
    3: SymbolKind.NAMESPACE,
    4: SymbolKind.PACKAGE,
    5: SymbolKind.CLASS,
    6: SymbolKind.METHOD,
    8: SymbolKind.FIELD,
    9: SymbolKind.METHOD,
    10: SymbolKind.ENUM,
    11: SymbolKind.INTERFACE,
    12: SymbolKind.FUNCTION,
    13: SymbolKind.VARIABLE,
    14: SymbolKind.CONSTANT,
    23: SymbolKind.STRUCT,
    26: SymbolKind.TYPE,
}

_ClientFactory = Callable[[LspServerSpec], tuple[Any, LspClient]]


class LspProvider:
    """Serve symbol and reference queries for languages without a SCIP indexer."""

    def __init__(
        self,
        repo_root: str | Path,
        spec: LspServerSpec,
        *,
        commit: str | None = None,
        client_factory: _ClientFactory = spawn,
    ) -> None:
        self._root = Path(repo_root)
        self._spec = spec
        self._commit = commit
        self._client_factory = client_factory
        self._process: Any = None
        self._client: LspClient | None = None
        self._started = False

    def is_available(self) -> bool:
        return self._ensure_client() is not None

    def repo_find_symbol(self, repo: RepoRef, name: str, *, limit: int = 50) -> list[Symbol]:
        raw = self._symbol_query(name)
        symbols = [self._symbol_from_information(item) for item in raw[:limit]]
        return [symbol for symbol in symbols if symbol is not None]

    def repo_find_references(
        self, repo: RepoRef, symbol: str, *, include_definition: bool = True
    ) -> list[Reference]:
        client = self._ensure_client()
        if client is None:
            return []
        location = self._definition_location(symbol)
        if location is None:
            return []
        try:
            raw = client.request(
                "textDocument/references",
                {
                    "textDocument": {"uri": location["uri"]},
                    "position": location["range"]["start"],
                    "context": {"includeDeclaration": include_definition},
                },
            )
        except LspError:
            return []
        short = _short_name(symbol)
        references: list[Reference] = []
        for item in raw if isinstance(raw, list) else []:
            parsed = self._location_from_lsp(item)
            if parsed is None:
                continue
            references.append(
                Reference(
                    symbol=short,
                    location=parsed,
                    is_definition=False,
                    provenance=Provenance(source=Source.LSP, confidence=_LSP_CONFIDENCE),
                )
            )
        return references

    def repo_get_index_status(self, repo: RepoRef) -> IndexStatus:
        available = self.is_available()
        return IndexStatus(
            repo=repo,
            indexed_commit=self._commit if available else None,
            is_fresh=available,
            languages=(self._spec.language_id,),
            detail=f"language server {self._spec.command[0]}",
        )

    def _symbol_query(self, name: str) -> list[dict[str, Any]]:
        client = self._ensure_client()
        if client is None:
            return []
        try:
            raw = client.request("workspace/symbol", {"query": _short_name(name)})
        except LspError:
            return []
        return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []

    def _definition_location(self, symbol: str) -> dict[str, Any] | None:
        fallback: dict[str, Any] | None = None
        for item in self._symbol_query(symbol):
            location = item.get("location")
            if not (isinstance(location, dict) and "uri" in location and "range" in location):
                continue
            uri = location.get("uri")
            if isinstance(uri, str) and urlparse(uri).scheme not in ("", "file"):
                continue
            if item.get("name") == symbol:
                return location
            if fallback is None:
                fallback = location
        return fallback

    def _symbol_from_information(self, item: dict[str, Any]) -> Symbol | None:
        location = self._location_from_lsp(item.get("location"))
        if location is None:
            return None
        return Symbol(
            name=str(item.get("name", "")),
            kind=_LSP_SYMBOL_KIND.get(item.get("kind"), SymbolKind.UNKNOWN),
            location=location,
            provenance=Provenance(source=Source.LSP, confidence=_LSP_CONFIDENCE),
            language=self._spec.language_id,
            container=item.get("containerName") or None,
        )

    def _location_from_lsp(self, raw: Any) -> Location | None:
        if not isinstance(raw, dict):
            return None
        path = self._relative_path(raw.get("uri"))
        if path is None:
            return None
        span = raw.get("range") or {}
        start = span.get("start") or {}
        end = span.get("end") or {}
        return Location(
            path=path,
            start_line=int(start.get("line", 0)) + 1,
            start_col=int(start.get("character", 0)),
            end_line=int(end["line"]) + 1 if "line" in end else None,
            end_col=end.get("character"),
        )

    def _relative_path(self, uri: Any) -> str | None:
        if not isinstance(uri, str) or not uri:
            return None
        parsed = urlparse(uri)
        if parsed.scheme and parsed.scheme != "file":
            return None
        path = Path(unquote(parsed.path))
        try:
            return path.resolve().relative_to(self._root.resolve()).as_posix()
        except ValueError:
            return path.as_posix()

    def _ensure_client(self) -> LspClient | None:
        if self._started:
            return self._client
        self._started = True
        try:
            self._process, self._client = self._client_factory(self._spec)
            self._client.initialize(self._root)
            self._open_documents(self._client)
        except (OSError, LspError):
            self._client = None
        return self._client

    def _open_documents(self, client: LspClient) -> None:
        pruned = {".git", ".terraform", "node_modules", "vendor"}
        opened = 0
        for suffix in self._spec.source_suffixes:
            for path in sorted(self._root.rglob(f"*{suffix}")):
                if pruned.intersection(path.parts) or not path.is_file():
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                except OSError:
                    continue
                client.notify(
                    "textDocument/didOpen",
                    {
                        "textDocument": {
                            "uri": path.resolve().as_uri(),
                            "languageId": self._spec.language_id,
                            "version": 1,
                            "text": text,
                        }
                    },
                )
                opened += 1
        if opened:
            self._await_symbols(client)

    def _await_symbols(self, client: LspClient) -> None:
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
