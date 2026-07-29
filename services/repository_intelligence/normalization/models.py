"""Normalized result shapes for the repository-intelligence provider interface."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Source(StrEnum):
    SCIP = "scip"
    LSP = "lsp"
    AST = "ast"
    TREE_SITTER = "tree_sitter"
    AST_GREP = "ast_grep"
    ZOEKT = "zoekt"
    TEXT_SEARCH = "text_search"
    BUILD_METADATA = "build_metadata"
    COVERAGE = "coverage"
    EMBEDDING = "embedding"
    HEURISTIC = "heuristic"
    CBM = "cbm"


NON_AUTHORITATIVE_SOURCES = frozenset({Source.EMBEDDING, Source.HEURISTIC})


class SymbolKind(StrEnum):
    MODULE = "module"
    NAMESPACE = "namespace"
    PACKAGE = "package"
    CLASS = "class"
    INTERFACE = "interface"
    STRUCT = "struct"
    FUNCTION = "function"
    METHOD = "method"
    FIELD = "field"
    VARIABLE = "variable"
    CONSTANT = "constant"
    TYPE = "type"
    ENUM = "enum"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RepoRef:
    repo: str
    commit_sha: str


@dataclass(frozen=True)
class Location:
    path: str
    start_line: int
    start_col: int = 0
    end_line: int | None = None
    end_col: int | None = None


@dataclass(frozen=True)
class Provenance:
    source: Source
    confidence: float

    @property
    def authoritative(self) -> bool:
        return self.source not in NON_AUTHORITATIVE_SOURCES


@dataclass(frozen=True)
class Symbol:
    name: str
    kind: SymbolKind
    location: Location
    provenance: Provenance
    language: str | None = None
    container: str | None = None
    signature: str | None = None


@dataclass(frozen=True)
class SearchHit:
    location: Location
    snippet: str
    score: float
    provenance: Provenance


@dataclass(frozen=True)
class Reference:
    symbol: str
    location: Location
    is_definition: bool
    provenance: Provenance


@dataclass(frozen=True)
class CallEdge:
    caller: Symbol
    callee: Symbol
    provenance: Provenance
    call_site: Location | None = None


@dataclass(frozen=True)
class RelatedTest:
    test_id: str
    location: Location
    covers: str
    provenance: Provenance


@dataclass(frozen=True)
class DependencyEdge:
    from_module: str
    to_module: str
    kind: str
    provenance: Provenance


@dataclass(frozen=True)
class ChangeImpact:
    changed: tuple[str, ...]
    impacted_symbols: tuple[Symbol, ...]
    impacted_files: tuple[str, ...]
    impacted_tests: tuple[RelatedTest, ...]
    provenance: Provenance


@dataclass(frozen=True)
class RecentChange:
    commit_sha: str
    summary: str
    path: str
    authored_at: str | None = None


@dataclass(frozen=True)
class ContextPackage:
    focus: str
    symbols: tuple[Symbol, ...]
    references: tuple[Reference, ...]
    related_tests: tuple[RelatedTest, ...]
    dependencies: tuple[DependencyEdge, ...]
    summary: str | None = None
    callers: tuple[CallEdge, ...] = ()
    callees: tuple[CallEdge, ...] = ()
    implementations: tuple[Symbol, ...] = ()
    structural: tuple[SearchHit, ...] = ()
    candidate_files: tuple[str, ...] = ()
    build_targets: tuple[str, ...] = ()
    config_files: tuple[str, ...] = ()
    repository: str = ""
    indexed_commit: str | None = None
    recent_changes: tuple[RecentChange, ...] = ()


@dataclass(frozen=True)
class IndexStatus:
    repo: RepoRef
    indexed_commit: str | None
    is_fresh: bool
    languages: tuple[str, ...]
    last_indexed_at: str | None = None
    detail: str | None = None
