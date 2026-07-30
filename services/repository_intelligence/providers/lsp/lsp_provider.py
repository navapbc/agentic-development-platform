"""Repository-intelligence provider backed by a Language Server (LSP fallback tier)."""

from __future__ import annotations

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
        short = symbol.rsplit(".", 1)[-1]
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
            raw = client.request("workspace/symbol", {"query": name.rsplit(".", 1)[-1]})
        except LspError:
            return []
        return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []

    def _definition_location(self, symbol: str) -> dict[str, Any] | None:
        for item in self._symbol_query(symbol):
            location = item.get("location")
            if isinstance(location, dict) and "uri" in location and "range" in location:
                return location
        return None

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
        except (OSError, LspError):
            self._client = None
        return self._client
