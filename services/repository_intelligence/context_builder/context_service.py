"""Repository Intelligence Service: freshness tracking and context-package composition.

Wraps the hybrid router plus the structural, code-intelligence, and build-metadata
backends into a warm service. Tracks the indexed commit, triggers a reindex when the
requested commit changes or the index reports stale, and composes an enriched
`ContextPackage` (router tool outputs plus ast-grep structural matches, CBM file-level
candidate files, build targets, and config files) for the planning stage to consume.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, TypeVar

from services.repository_intelligence.context_builder.focus import (
    extract_file_paths,
    extract_focus_terms,
)
from services.repository_intelligence.indexing.reindex import reindex_repo
from services.repository_intelligence.indexing.scip_builder import resolve_commit, scip_indexer_for
from services.repository_intelligence.languages import Language, primary_language
from services.repository_intelligence.normalization.models import (
    ContextPackage,
    RecentChange,
    RepoRef,
)
from services.repository_intelligence.providers.ast_grep.ast_grep_provider import (
    AstGrepProvider,
    hit_from_match,
)
from services.repository_intelligence.providers.build_metadata.build_metadata_provider import (
    BuildMetadataProvider,
)
from services.repository_intelligence.providers.cbm.cbm_provider import CBMProvider
from services.repository_intelligence.providers.coverage.coverage_provider import CoverageProvider
from services.repository_intelligence.providers.custom.python_provider import CustomPythonProvider
from services.repository_intelligence.providers.lsp.lsp_client import server_spec_for
from services.repository_intelligence.providers.lsp.lsp_provider import LspProvider
from services.repository_intelligence.providers.router import RouterProvider
from services.repository_intelligence.providers.scip.scip_provider import ScipProvider
from services.repository_intelligence.providers.zoekt.zoekt_provider import ZoektProvider

_T = TypeVar("_T")

_CONFIG_FILENAMES = frozenset(
    {
        "pyproject.toml",
        "setup.cfg",
        "setup.py",
        "tox.ini",
        "pytest.ini",
        ".coveragerc",
        "Makefile",
        "poetry.lock",
        "uv.lock",
    }
)
_RECENT_PATHS_LIMIT = 5
_RECENT_CHANGES_PER_PATH = 5
_MAX_QUERY_TERMS = 5
_CANDIDATE_FILE_BUDGET = 200
_GIT_FIELD_SEP = "\x1f"


class RepositoryIntelligenceService:
    """Warm service composing commit-specific context packages."""

    def __init__(
        self,
        router: RouterProvider,
        *,
        repo_root: str | Path,
        ast_grep: AstGrepProvider | None = None,
        cbm: CBMProvider | None = None,
        build_metadata: BuildMetadataProvider | None = None,
        reindex: Callable[[RepoRef, str | None], None] | None = None,
    ) -> None:
        self._router = router
        self._root = Path(repo_root)
        self._ast_grep = ast_grep
        self._cbm = cbm
        self._build_metadata = build_metadata
        self._reindex = reindex
        self._indexed_commit: str | None = None
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
    ) -> RepositoryIntelligenceService:
        root = Path(repo_root)
        language = primary_language(root)
        python = language is Language.PYTHON
        indexer = scip_indexer_for(language)
        lsp_spec = server_spec_for(language)
        ast_grep = AstGrepProvider(root)
        build_metadata = BuildMetadataProvider(root)
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
        router = RouterProvider(
            scip=ScipProvider(root, commit=commit, index_root=index_root, indexer=indexer)
            if indexer is not None
            else None,
            zoekt=ZoektProvider(root, commit=commit, index_root=index_root),
            coverage=CoverageProvider(root, venv_python=venv_python) if python else None,
            build_metadata=build_metadata,
            custom=CustomPythonProvider(root) if python else None,
            ast_grep=ast_grep,
            cbm=cbm,
            lsp=LspProvider(root, lsp_spec, commit=commit) if lsp_spec is not None else None,
        )

        def reindex(repo: RepoRef, prior_commit: str | None) -> None:
            reindex_repo(
                root, repo, prior_commit=prior_commit, index_root=index_root, indexer=indexer
            )

        return cls(
            router,
            repo_root=root,
            ast_grep=ast_grep,
            cbm=cbm,
            build_metadata=build_metadata,
            reindex=reindex if indexer is not None else None,
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

    def _resolve_commit(self, repo: RepoRef) -> RepoRef:
        if repo.commit_sha:
            return repo
        head = self._safe(lambda: resolve_commit(self._root), "")
        return replace(repo, commit_sha=head) if head else repo

    def ensure_fresh(self, repo: RepoRef) -> bool:
        repo = self._resolve_commit(repo)
        status = self._safe(lambda: self._router.repo_get_index_status(repo), None)
        if status is not None and status.is_fresh and status.indexed_commit == repo.commit_sha:
            self._indexed_commit = status.indexed_commit
            return True
        prior = self._indexed_commit or (status.indexed_commit if status is not None else None)
        if self._reindex is not None:
            self._reindex(repo, prior)
        self._indexed_commit = repo.commit_sha
        return False

    def context_package(self, repo: RepoRef, focus: str, detail: str = "") -> ContextPackage:
        repo = self._resolve_commit(repo)
        self.ensure_fresh(repo)
        base = self._router.repo_get_context_package(repo, focus)
        if detail or any(char.isspace() for char in focus):
            text = f"{focus}\n{detail}" if detail else focus
            terms = (extract_focus_terms(text) or [focus])[:_MAX_QUERY_TERMS]
            explicit = self._existing_paths(extract_file_paths(text))
            symbols = self._symbols_for(repo, terms)
            references = self._references_for(repo, terms)
            derived = {ref.location.path for ref in references if ref.location.path}
            derived |= {symbol.location.path for symbol in symbols if symbol.location.path}
            candidate_files = sorted(derived | set(explicit))
        else:
            symbols = list(base.symbols)
            references = list(base.references)
            candidate_files = self._candidate_files(repo, [focus])
        focus_paths = self._focus_paths(symbols, candidate_files)
        return replace(
            base,
            symbols=tuple(symbols),
            references=tuple(references),
            callers=tuple(self._safe(lambda: self._router.repo_get_callers(repo, focus), [])),
            callees=tuple(self._safe(lambda: self._router.repo_get_callees(repo, focus), [])),
            implementations=tuple(
                self._safe(lambda: self._router.repo_find_implementations(repo, focus), [])
            ),
            structural=tuple(self._structural()),
            candidate_files=tuple(candidate_files),
            build_targets=tuple(self._build_targets()),
            config_files=tuple(self._config_files()),
            repository=repo.repo,
            indexed_commit=self._indexed_commit,
            recent_changes=tuple(self._recent_changes(focus_paths)),
        )

    @staticmethod
    def _focus_paths(symbols: list[Any], candidate_files: list[str]) -> list[str]:
        paths = {symbol.location.path for symbol in symbols if symbol.location.path}
        paths.update(candidate_files)
        return sorted(paths)[:_RECENT_PATHS_LIMIT]

    def _existing_paths(self, paths: list[str]) -> list[str]:
        return [path for path in paths if (self._root / path).is_file()]

    def _symbols_for(self, repo: RepoRef, terms: list[str]) -> list[Any]:
        out: list[Any] = []
        seen: set[tuple[str, str, int]] = set()
        for term in terms:
            for symbol in self._safe(lambda t=term: self._router.repo_find_symbol(repo, t), []):
                key = (symbol.name, symbol.location.path, symbol.location.start_line)
                if key not in seen:
                    seen.add(key)
                    out.append(symbol)
        return out

    def _references_for(self, repo: RepoRef, terms: list[str]) -> list[Any]:
        out: list[Any] = []
        seen: set[tuple[str, str, int, bool]] = set()
        for term in terms:
            for ref in self._safe(lambda t=term: self._router.repo_find_references(repo, t), []):
                key = (ref.symbol, ref.location.path, ref.location.start_line, ref.is_definition)
                if key not in seen:
                    seen.add(key)
                    out.append(ref)
            if len(out) >= _CANDIDATE_FILE_BUDGET:
                break
        return out

    def _recent_changes(self, paths: list[str]) -> list[RecentChange]:
        changes: list[RecentChange] = []
        for path in paths:
            output = self._safe(lambda p=path: self._git_log(p), "")
            for line in output.splitlines():
                parts = line.split(_GIT_FIELD_SEP)
                if len(parts) != 3:
                    continue
                sha, summary, authored = parts
                changes.append(
                    RecentChange(
                        commit_sha=sha,
                        summary=summary,
                        path=path,
                        authored_at=authored or None,
                    )
                )
        return changes

    def _git_log(self, path: str) -> str:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(self._root),
                "log",
                f"--max-count={_RECENT_CHANGES_PER_PATH}",
                f"--format=%H{_GIT_FIELD_SEP}%s{_GIT_FIELD_SEP}%aI",
                "--",
                path,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return completed.stdout

    def _structural(self):
        if self._ast_grep is None:
            return []
        matches = []
        for finder in (
            self._ast_grep.find_routes,
            self._ast_grep.find_models,
            self._ast_grep.find_raise_sites,
        ):
            matches.extend(self._safe(finder, []))
        hits = [hit_from_match(match) for match in matches]
        return sorted(
            hits, key=lambda hit: (hit.location.path, hit.location.start_line, hit.snippet)
        )

    def _candidate_files(self, repo: RepoRef, terms: list[str]) -> list[str]:
        if not self._cbm_ready():
            return []
        found: set[str] = set()
        for term in terms:
            refs = self._safe(lambda t=term: self._cbm.candidate_references(repo, t), [])
            found.update(ref.location.path for ref in refs if ref.location.path)
            if len(found) >= _CANDIDATE_FILE_BUDGET:
                break
        return sorted(found)

    def _build_targets(self) -> list[str]:
        if self._build_metadata is None:
            return []
        targets = self._safe(self._build_metadata.build_targets, [])
        return [f"{target.kind}:{target.name}" for target in targets]

    def _config_files(self) -> list[str]:
        names = {name for name in _CONFIG_FILENAMES if (self._root / name).is_file()}
        names.update(path.name for path in self._root.glob("requirements*.txt"))
        return sorted(names)
