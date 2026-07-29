"""Repository-intelligence provider backed by a SCIP index (scip-python)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from services.repository_intelligence.indexing.scip_builder import (
    DEFAULT_SCIP_CLI,
    DEFAULT_SCIP_PYTHON_CMD,
    build_scip_index,
    resolve_commit,
)
from services.repository_intelligence.indexing.scip_index import (
    Occurrence,
    ScipIndex,
    SymbolInfo,
    parse_symbol,
)
from services.repository_intelligence.normalization.models import (
    CallEdge,
    ChangeImpact,
    ContextPackage,
    DependencyEdge,
    IndexStatus,
    Location,
    Provenance,
    Reference,
    RelatedTest,
    RepoRef,
    SearchHit,
    Source,
    Symbol,
    SymbolKind,
)

_DEFINITION_KINDS = frozenset({"class", "function", "method", "variable"})
_CALLABLE_KINDS = frozenset({"class", "function", "method"})

_SCIP_KIND = {
    "class": SymbolKind.CLASS,
    "function": SymbolKind.FUNCTION,
    "method": SymbolKind.METHOD,
    "variable": SymbolKind.VARIABLE,
    "field": SymbolKind.FIELD,
    "module": SymbolKind.MODULE,
    "type": SymbolKind.TYPE,
    "type_parameter": SymbolKind.TYPE,
    "parameter": SymbolKind.VARIABLE,
}

_SCIP_CONFIDENCE = 0.98

_IGNORED_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
        "build",
        "dist",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".tox",
        ".eggs",
    }
)


class ScipProvider:
    """Answers structural queries from a SCIP index with line-level provenance."""

    def __init__(
        self,
        repo_root: str | Path,
        *,
        index: ScipIndex | None = None,
        commit: str | None = None,
        index_root: Path | None = None,
        scip_python_cmd: str = DEFAULT_SCIP_PYTHON_CMD,
        scip_cli: str = DEFAULT_SCIP_CLI,
    ) -> None:
        self._root = Path(repo_root).resolve()
        self._index_root = index_root
        self._scip_python_cmd = scip_python_cmd
        self._scip_cli = scip_cli
        self._index = index
        self._commit = commit
        self._by_qualified: dict[str, SymbolInfo] = {}
        self._by_short: dict[str, list[SymbolInfo]] = {}
        if index is not None:
            self._build_lookups(index)

    def _ensure_index(self) -> ScipIndex:
        if self._index is None:
            self._index, self._commit = build_scip_index(
                self._root,
                index_root=self._index_root,
                scip_python_cmd=self._scip_python_cmd,
                scip_cli=self._scip_cli,
            )
            self._build_lookups(self._index)
        return self._index

    def _build_lookups(self, index: ScipIndex) -> None:
        self._by_qualified = {}
        self._by_short = {}
        for info in index.project_symbols(_DEFINITION_KINDS):
            if info.qualified:
                self._by_qualified[info.qualified] = info
            if info.name:
                self._by_short.setdefault(info.name, []).append(info)

    def _resolve(self, symbol: str) -> list[SymbolInfo]:
        exact = self._by_qualified.get(symbol)
        if exact is not None:
            return [exact]
        short = symbol.rsplit(".", 1)[-1]
        return list(self._by_short.get(short, []))

    def _location(self, occ: Occurrence) -> Location:
        return Location(
            path=occ.path,
            start_line=occ.start_line,
            start_col=occ.start_col,
            end_line=occ.end_line,
            end_col=occ.end_col,
        )

    def _to_symbol(self, index: ScipIndex, info: SymbolInfo) -> Symbol:
        occ = index.definition_of(info.symbol)
        location = self._location(occ) if occ is not None else Location(path="", start_line=0)
        return Symbol(
            name=info.qualified or info.name or info.symbol,
            kind=_SCIP_KIND.get(info.kind, SymbolKind.UNKNOWN),
            location=location,
            provenance=Provenance(source=Source.SCIP, confidence=_SCIP_CONFIDENCE),
            language="python",
            container=info.module,
        )

    def repo_search_code(self, repo: RepoRef, query: str, *, limit: int = 50) -> list[SearchHit]:
        try:
            pattern = re.compile(query)
        except re.error:
            pattern = re.compile(re.escape(query))
        provenance = Provenance(source=Source.TEXT_SEARCH, confidence=1.0)
        hits: list[SearchHit] = []
        for path in self._python_files():
            rel = path.relative_to(self._root).as_posix()
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError:
                continue
            for lineno, line in enumerate(lines, start=1):
                match = pattern.search(line)
                if match is None:
                    continue
                hits.append(
                    SearchHit(
                        location=Location(path=rel, start_line=lineno, start_col=match.start()),
                        snippet=line.strip(),
                        score=1.0,
                        provenance=provenance,
                    )
                )
                if len(hits) >= limit:
                    return hits
        return hits

    def repo_find_symbol(self, repo: RepoRef, name: str, *, limit: int = 50) -> list[Symbol]:
        index = self._ensure_index()
        short = name.rsplit(".", 1)[-1]
        infos = self._by_short.get(short, [])
        return [self._to_symbol(index, info) for info in infos[:limit]]

    def repo_find_references(
        self, repo: RepoRef, symbol: str, *, include_definition: bool = True
    ) -> list[Reference]:
        index = self._ensure_index()
        references: list[Reference] = []
        seen: set[tuple[str, int, int]] = set()
        for info in self._resolve(symbol):
            short = info.name or symbol.rsplit(".", 1)[-1]
            provenance = Provenance(source=Source.SCIP, confidence=_SCIP_CONFIDENCE)
            if include_definition:
                definition = index.definition_of(info.symbol)
                if definition is not None:
                    key = (definition.path, definition.start_line, definition.start_col)
                    if key not in seen:
                        seen.add(key)
                        references.append(
                            Reference(
                                symbol=short,
                                location=self._location(definition),
                                is_definition=True,
                                provenance=provenance,
                            )
                        )
            for occ in index.references_of(info.symbol):
                key = (occ.path, occ.start_line, occ.start_col)
                if key in seen:
                    continue
                seen.add(key)
                references.append(
                    Reference(
                        symbol=short,
                        location=self._location(occ),
                        is_definition=False,
                        provenance=provenance,
                    )
                )
        return references

    def repo_find_implementations(self, repo: RepoRef, symbol: str) -> list[Symbol]:
        index = self._ensure_index()
        short = symbol.rsplit(".", 1)[-1]
        results: list[Symbol] = []
        for base_symbol, impls in index.base_symbols().items():
            base_info = index.symbols.get(base_symbol) or parse_symbol(base_symbol, index.project)
            if base_info.name != short and base_info.qualified != symbol:
                continue
            for info in impls:
                results.append(self._to_symbol(index, info))
        return results

    def repo_get_callers(self, repo: RepoRef, symbol: str) -> list[CallEdge]:
        index = self._ensure_index()
        provenance = Provenance(source=Source.SCIP, confidence=_SCIP_CONFIDENCE)
        edges: list[CallEdge] = []
        seen: set[tuple] = set()
        for info in self._resolve(symbol):
            callee = self._to_symbol(index, info)
            for enclosing_symbol, occ in index.callers_of(info.symbol):
                caller, key = self._caller_symbol(index, enclosing_symbol, occ)
                if key in seen:
                    continue
                seen.add(key)
                edges.append(
                    CallEdge(
                        caller=caller,
                        callee=callee,
                        provenance=provenance,
                        call_site=self._location(occ),
                    )
                )
        return edges

    def _caller_symbol(
        self, index: ScipIndex, enclosing_symbol: str | None, occ: Occurrence
    ) -> tuple[Symbol, tuple]:
        provenance = Provenance(source=Source.SCIP, confidence=_SCIP_CONFIDENCE)
        if enclosing_symbol is not None:
            definition = index.definition_of(enclosing_symbol)
            info = index.symbols.get(enclosing_symbol)
            if definition is not None and info is not None:
                caller = Symbol(
                    name=info.qualified or info.name or enclosing_symbol,
                    kind=_SCIP_KIND.get(info.kind, SymbolKind.UNKNOWN),
                    location=self._location(definition),
                    provenance=provenance,
                    language="python",
                    container=info.module,
                )
                return caller, ("def", enclosing_symbol)
        caller = Symbol(
            name="",
            kind=SymbolKind.MODULE,
            location=Location(path=occ.path, start_line=occ.start_line),
            provenance=provenance,
            language="python",
        )
        return caller, ("module", occ.path, occ.start_line)

    def repo_get_callees(self, repo: RepoRef, symbol: str) -> list[CallEdge]:
        index = self._ensure_index()
        provenance = Provenance(source=Source.SCIP, confidence=_SCIP_CONFIDENCE)
        edges: list[CallEdge] = []
        seen: set[str] = set()
        for info in self._resolve(symbol):
            definition = index.definition_of(info.symbol)
            if definition is None or definition.enclosing is None:
                continue
            caller = self._to_symbol(index, info)
            start_line, start_col, end_line, end_col = definition.enclosing
            for occ in index.occurrences:
                if occ.path != definition.path or occ.is_definition:
                    continue
                if not self._within(occ, start_line, start_col, end_line, end_col):
                    continue
                target = index.symbols.get(occ.symbol)
                if target is None or not target.is_project or target.kind not in _CALLABLE_KINDS:
                    continue
                if index.definition_of(occ.symbol) is None or occ.symbol in seen:
                    continue
                seen.add(occ.symbol)
                edges.append(
                    CallEdge(
                        caller=caller,
                        callee=self._to_symbol(index, target),
                        provenance=provenance,
                        call_site=self._location(occ),
                    )
                )
        return edges

    @staticmethod
    def _within(
        occ: Occurrence, start_line: int, start_col: int, end_line: int, end_col: int
    ) -> bool:
        return (start_line, start_col) <= (occ.start_line, occ.start_col) <= (end_line, end_col)

    def repo_get_related_tests(self, repo: RepoRef, target: str) -> list[RelatedTest]:
        self._ensure_index()
        return []

    def repo_get_module_dependencies(self, repo: RepoRef, module: str) -> list[DependencyEdge]:
        index = self._ensure_index()
        name = self._strip_project_prefix(index.project, module)
        provenance = Provenance(source=Source.SCIP, confidence=_SCIP_CONFIDENCE)
        edges: list[DependencyEdge] = []
        for dep, is_external in index.module_dependencies().get(name, []):
            edges.append(
                DependencyEdge(
                    from_module=name,
                    to_module=dep,
                    kind="external_import" if is_external else "import",
                    provenance=provenance,
                )
            )
        return edges

    @staticmethod
    def _strip_project_prefix(project: str, module: str) -> str:
        prefix = f"{project}."
        return module[len(prefix) :] if module.startswith(prefix) else module

    def repo_get_change_impact(self, repo: RepoRef, changed: list[str]) -> ChangeImpact:
        index = self._ensure_index()
        provenance = Provenance(source=Source.SCIP, confidence=_SCIP_CONFIDENCE)
        changed_modules = {self._path_to_module(index, entry) for entry in changed}
        changed_modules.discard("")
        dependents = index.module_dependencies()
        impacted_files: set[str] = set()
        impacted_symbols: list[Symbol] = []
        for owner, edges in dependents.items():
            if any(dep in changed_modules for dep, _ in edges):
                for info in index.project_symbols(_DEFINITION_KINDS):
                    if info.module == owner:
                        impacted_symbols.append(self._to_symbol(index, info))
                        occ = index.definition_of(info.symbol)
                        if occ is not None:
                            impacted_files.add(occ.path)
        return ChangeImpact(
            changed=tuple(changed),
            impacted_symbols=tuple(impacted_symbols),
            impacted_files=tuple(sorted(impacted_files)),
            impacted_tests=(),
            provenance=provenance,
        )

    def _path_to_module(self, index: ScipIndex, entry: str) -> str:
        candidate = entry.replace("\\", "/")
        for occ in index.occurrences:
            if occ.path == candidate:
                info = index.symbols.get(occ.symbol)
                if info is not None and info.kind == "module" and info.module:
                    return info.module
        return self._strip_project_prefix(index.project, entry)

    def repo_get_context_package(self, repo: RepoRef, focus: str) -> ContextPackage:
        symbols = self.repo_find_symbol(repo, focus)
        references = self.repo_find_references(repo, focus, include_definition=False)
        dependencies: list[DependencyEdge] = []
        if symbols and symbols[0].container:
            dependencies = self.repo_get_module_dependencies(repo, symbols[0].container)
        return ContextPackage(
            focus=focus,
            symbols=tuple(symbols),
            references=tuple(references),
            related_tests=(),
            dependencies=tuple(dependencies),
        )

    def repo_get_index_status(self, repo: RepoRef) -> IndexStatus:
        index = self._ensure_index()
        detail = (
            f"{len(index.documents)} modules, {len(self._by_qualified)} symbols, "
            f"{len(index.occurrences)} occurrences"
        )
        return IndexStatus(
            repo=repo,
            indexed_commit=self._commit,
            is_fresh=self._is_fresh(),
            languages=("python",),
            detail=detail,
        )

    def _is_fresh(self) -> bool:
        if self._commit is None:
            return False
        try:
            head = resolve_commit(self._root)
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False
        return head.startswith(self._commit) or self._commit.startswith(head)

    def _python_files(self) -> list[Path]:
        files: list[Path] = []
        for path in self._root.rglob("*.py"):
            if any(part in _IGNORED_DIRS for part in path.relative_to(self._root).parts):
                continue
            files.append(path)
        return sorted(files)
