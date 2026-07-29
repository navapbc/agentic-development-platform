"""Hybrid router implementing the repository-intelligence provider interface.

Delegates each tool to the authoritative backend per the R2 routing table and
merges multi-source results with provenance and confidence. Optional backends
(Zoekt, CBM) degrade gracefully: the router falls back to SCIP when they are
unavailable. Gate-scored tools (symbol definition, references, callers, related
tests) keep their authoritative source's precision; CBM file-level candidates
are used only as a fallback when the authoritative source yields nothing.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from services.repository_intelligence.normalization.models import (
    CallEdge,
    ChangeImpact,
    ContextPackage,
    DependencyEdge,
    IndexStatus,
    Provenance,
    Reference,
    RelatedTest,
    RepoRef,
    SearchHit,
    Source,
    Symbol,
)
from services.repository_intelligence.providers.ast_grep.ast_grep_provider import AstGrepProvider
from services.repository_intelligence.providers.build_metadata.build_metadata_provider import (
    BuildMetadataProvider,
)
from services.repository_intelligence.providers.cbm.cbm_provider import CBMProvider
from services.repository_intelligence.providers.coverage.coverage_provider import CoverageProvider
from services.repository_intelligence.providers.custom.python_provider import CustomPythonProvider
from services.repository_intelligence.providers.scip.scip_provider import ScipProvider
from services.repository_intelligence.providers.zoekt.zoekt_provider import ZoektProvider

_T = TypeVar("_T")


def _authoritative(items: list[_T]) -> list[_T]:
    return [item for item in items if item.provenance.authoritative]  # type: ignore[attr-defined]


def _sorted_references(refs: list[Reference]) -> list[Reference]:
    return sorted(
        refs,
        key=lambda ref: (ref.location.path, ref.location.start_line, 0 if ref.is_definition else 1),
    )


def _sorted_callers(edges: list[CallEdge]) -> list[CallEdge]:
    return sorted(
        edges, key=lambda edge: (edge.caller.location.path, edge.caller.location.start_line)
    )


class RouterProvider:
    """Routes each repository-intelligence tool to its authoritative backend."""

    def __init__(
        self,
        *,
        scip: ScipProvider,
        zoekt: ZoektProvider | None = None,
        coverage: CoverageProvider | None = None,
        build_metadata: BuildMetadataProvider | None = None,
        custom: CustomPythonProvider | None = None,
        cbm: CBMProvider | None = None,
        ast_grep: AstGrepProvider | None = None,
    ) -> None:
        self._scip = scip
        self._zoekt = zoekt
        self._coverage = coverage
        self._build_metadata = build_metadata
        self._custom = custom
        self._cbm = cbm
        self._ast_grep = ast_grep
        self._cbm_available: bool | None = None

    @classmethod
    def from_repo(
        cls,
        repo_root: str | Path,
        *,
        commit: str | None = None,
        index_root: Path | None = None,
        venv_python: str | Path | None = None,
        cbm_binary: str | Path | None = None,
        cbm_allowed_root: str | Path | None = None,
        cbm_cache_dir: str | Path | None = None,
    ) -> RouterProvider:
        root = Path(repo_root)
        cbm = (
            CBMProvider(
                root,
                cbm_binary,
                allowed_root=cbm_allowed_root or root,
                cache_dir=cbm_cache_dir,
            )
            if cbm_binary is not None
            else None
        )
        return cls(
            scip=ScipProvider(root, commit=commit, index_root=index_root),
            zoekt=ZoektProvider(root, commit=commit, index_root=index_root),
            coverage=CoverageProvider(root, venv_python=venv_python),
            build_metadata=BuildMetadataProvider(root),
            custom=CustomPythonProvider(root),
            ast_grep=AstGrepProvider(root),
            cbm=cbm,
        )

    @staticmethod
    def _safe(fn: Callable[[], _T], default: _T) -> _T:
        try:
            return fn()
        except Exception:  # noqa: BLE001
            return default

    def _cbm_ready(self) -> bool:
        if self._cbm is None:
            return False
        if self._cbm_available is None:
            self._cbm_available = self._safe(self._cbm.is_available, False)
        return self._cbm_available

    def repo_search_code(self, repo: RepoRef, query: str, *, limit: int = 50) -> list[SearchHit]:
        if self._zoekt is not None:
            hits = self._safe(lambda: self._zoekt.repo_search_code(repo, query, limit=limit), None)
            if hits is not None:
                return hits
        return self._safe(lambda: self._scip.repo_search_code(repo, query, limit=limit), [])

    def repo_find_symbol(self, repo: RepoRef, name: str, *, limit: int = 50) -> list[Symbol]:
        return _authoritative(
            self._safe(lambda: self._scip.repo_find_symbol(repo, name, limit=limit), [])
        )

    def repo_find_references(
        self, repo: RepoRef, symbol: str, *, include_definition: bool = True
    ) -> list[Reference]:
        primary = _authoritative(
            self._safe(
                lambda: self._scip.repo_find_references(
                    repo, symbol, include_definition=include_definition
                ),
                [],
            )
        )
        if primary:
            return _sorted_references(primary)
        if self._cbm_ready():
            candidates = _authoritative(
                self._safe(lambda: self._cbm.candidate_references(repo, symbol), [])
            )
            return _sorted_references(candidates)
        return []

    def repo_find_implementations(self, repo: RepoRef, symbol: str) -> list[Symbol]:
        return _authoritative(
            self._safe(lambda: self._scip.repo_find_implementations(repo, symbol), [])
        )

    def repo_get_callers(self, repo: RepoRef, symbol: str) -> list[CallEdge]:
        primary = _authoritative(self._safe(lambda: self._scip.repo_get_callers(repo, symbol), []))
        if primary:
            return _sorted_callers(primary)
        if self._cbm_ready():
            candidates = _authoritative(
                self._safe(lambda: self._cbm.candidate_callers(repo, symbol), [])
            )
            return _sorted_callers(candidates)
        return []

    def repo_get_callees(self, repo: RepoRef, symbol: str) -> list[CallEdge]:
        return _authoritative(self._safe(lambda: self._scip.repo_get_callees(repo, symbol), []))

    def repo_get_related_tests(self, repo: RepoRef, target: str) -> list[RelatedTest]:
        if self._coverage is not None:
            return _authoritative(
                self._safe(lambda: self._coverage.repo_get_related_tests(repo, target), [])
            )
        if self._cbm_ready():
            return _authoritative(
                self._safe(lambda: self._cbm.repo_get_related_tests(repo, target), [])
            )
        return []

    def repo_get_module_dependencies(self, repo: RepoRef, module: str) -> list[DependencyEdge]:
        edges: list[DependencyEdge] = []
        if self._custom is not None:
            edges += self._safe(lambda: self._custom.repo_get_module_dependencies(repo, module), [])
        if self._build_metadata is not None:
            edges += self._safe(
                lambda: self._build_metadata.repo_get_module_dependencies(repo, ""), []
            )
        unique: dict[tuple[str, str, str], DependencyEdge] = {}
        for edge in _authoritative(edges):
            unique.setdefault((edge.from_module, edge.to_module, edge.kind), edge)
        return sorted(
            unique.values(), key=lambda edge: (edge.to_module, edge.kind, edge.from_module)
        )

    def repo_get_change_impact(self, repo: RepoRef, changed: list[str]) -> ChangeImpact:
        base = self._safe(lambda: self._scip.repo_get_change_impact(repo, changed), None)
        if base is None:
            base = ChangeImpact(
                changed=tuple(changed),
                impacted_symbols=(),
                impacted_files=(),
                impacted_tests=(),
                provenance=Provenance(source=Source.SCIP, confidence=0.0),
            )
        tests = list(base.impacted_tests)
        seen = {(test.test_id, test.location.path) for test in tests}
        for target in changed:
            for test in self.repo_get_related_tests(repo, target):
                key = (test.test_id, test.location.path)
                if key not in seen:
                    seen.add(key)
                    tests.append(test)
        return ChangeImpact(
            changed=base.changed,
            impacted_symbols=base.impacted_symbols,
            impacted_files=base.impacted_files,
            impacted_tests=tuple(tests),
            provenance=base.provenance,
        )

    def repo_get_context_package(self, repo: RepoRef, focus: str) -> ContextPackage:
        return ContextPackage(
            focus=focus,
            symbols=tuple(self.repo_find_symbol(repo, focus)),
            references=tuple(self.repo_find_references(repo, focus)),
            related_tests=tuple(self.repo_get_related_tests(repo, focus)),
            dependencies=tuple(self.repo_get_module_dependencies(repo, focus)),
        )

    def repo_get_index_status(self, repo: RepoRef) -> IndexStatus:
        if self._cbm_ready():
            status = self._safe(lambda: self._cbm.repo_get_index_status(repo), None)
            if status is not None and status.indexed_commit is not None:
                return status
        status = self._safe(lambda: self._scip.repo_get_index_status(repo), None)
        if status is not None:
            return status
        if self._zoekt is not None:
            status = self._safe(lambda: self._zoekt.repo_get_index_status(repo), None)
            if status is not None:
                return status
        return IndexStatus(repo=repo, indexed_commit=None, is_fresh=False, languages=("python",))
