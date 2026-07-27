"""Shared provider interface for repository-intelligence backends."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from services.repository_intelligence.normalization.models import (
    CallEdge,
    ChangeImpact,
    ContextPackage,
    DependencyEdge,
    IndexStatus,
    Reference,
    RelatedTest,
    RepoRef,
    SearchHit,
    Symbol,
)


@runtime_checkable
class RepositoryIntelligenceProvider(Protocol):
    def repo_search_code(
        self, repo: RepoRef, query: str, *, limit: int = 50
    ) -> list[SearchHit]: ...

    def repo_find_symbol(self, repo: RepoRef, name: str, *, limit: int = 50) -> list[Symbol]: ...

    def repo_find_references(
        self, repo: RepoRef, symbol: str, *, include_definition: bool = True
    ) -> list[Reference]: ...

    def repo_find_implementations(self, repo: RepoRef, symbol: str) -> list[Symbol]: ...

    def repo_get_callers(self, repo: RepoRef, symbol: str) -> list[CallEdge]: ...

    def repo_get_callees(self, repo: RepoRef, symbol: str) -> list[CallEdge]: ...

    def repo_get_related_tests(self, repo: RepoRef, target: str) -> list[RelatedTest]: ...

    def repo_get_module_dependencies(self, repo: RepoRef, module: str) -> list[DependencyEdge]: ...

    def repo_get_change_impact(self, repo: RepoRef, changed: list[str]) -> ChangeImpact: ...

    def repo_get_context_package(self, repo: RepoRef, focus: str) -> ContextPackage: ...

    def repo_get_index_status(self, repo: RepoRef) -> IndexStatus: ...
