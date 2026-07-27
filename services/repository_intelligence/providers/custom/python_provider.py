"""Repository-intelligence provider backed by a stdlib-ast index of a Python repo."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

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

_KIND_BY_NODE = {
    ast.FunctionDef: SymbolKind.FUNCTION,
    ast.AsyncFunctionDef: SymbolKind.FUNCTION,
    ast.ClassDef: SymbolKind.CLASS,
}


@dataclass(frozen=True)
class _Def:
    qname: str
    name: str
    kind: SymbolKind
    location: Location
    container: str | None
    bases: tuple[str, ...] = ()


@dataclass
class _Index:
    project: str
    defs: list[_Def] = field(default_factory=list)
    defs_by_qname: dict[str, _Def] = field(default_factory=dict)
    defs_by_name: dict[str, list[_Def]] = field(default_factory=dict)
    calls: list[tuple[str | None, str, Location]] = field(default_factory=list)
    imports: list[tuple[str, str, Location]] = field(default_factory=list)
    occurrences: dict[str, list[Location]] = field(default_factory=dict)
    modules: dict[str, str] = field(default_factory=dict)


class _ModuleVisitor(ast.NodeVisitor):
    def __init__(self, index: _Index, module_qname: str, rel_path: str) -> None:
        self._index = index
        self._module = module_qname
        self._rel_path = rel_path
        self._scope: list[str] = [module_qname]
        self._func_depth = 0

    def _loc(self, node: ast.AST) -> Location:
        return Location(
            path=self._rel_path,
            start_line=getattr(node, "lineno", 1),
            start_col=getattr(node, "col_offset", 0),
            end_line=getattr(node, "end_lineno", None),
            end_col=getattr(node, "end_col_offset", None),
        )

    def _record_def(
        self, node: ast.AST, name: str, kind: SymbolKind, bases: tuple[str, ...]
    ) -> str:
        container = self._scope[-1]
        qname = f"{container}.{name}"
        definition = _Def(
            qname=qname,
            name=name,
            kind=kind,
            location=self._loc(node),
            container=container,
            bases=bases,
        )
        self._index.defs.append(definition)
        self._index.defs_by_qname[qname] = definition
        self._index.defs_by_name.setdefault(name, []).append(definition)
        return qname

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        bases = tuple(self._name_of(base) for base in node.bases if self._name_of(base))
        qname = self._record_def(node, node.name, SymbolKind.CLASS, bases)
        self._scope.append(qname)
        self.generic_visit(node)
        self._scope.pop()

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        parent = self._index.defs_by_qname.get(self._scope[-1])
        kind = (
            SymbolKind.METHOD
            if parent is not None and parent.kind is SymbolKind.CLASS
            else SymbolKind.FUNCTION
        )
        qname = self._record_def(node, node.name, kind, ())
        self._scope.append(qname)
        self._func_depth += 1
        self.generic_visit(node)
        self._func_depth -= 1
        self._scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._index.imports.append((self._module, alias.name, self._loc(node)))
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        target = node.module or ""
        if node.level and target:
            target = f"{'.' * node.level}{target}"
        elif node.level:
            target = "." * node.level
        if target:
            self._index.imports.append((self._module, target, self._loc(node)))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        callee = self._name_of(node.func)
        if callee:
            caller = self._scope[-1] if self._func_depth else None
            self._index.calls.append((caller, callee, self._loc(node.func)))
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        self._index.occurrences.setdefault(node.id, []).append(self._loc(node))
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self._index.occurrences.setdefault(node.attr, []).append(self._loc(node))
        self.generic_visit(node)

    @staticmethod
    def _name_of(node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        return None


class CustomPythonProvider:
    def __init__(self, repo_root: str | Path) -> None:
        self._root = Path(repo_root).resolve()
        self._index: _Index | None = None

    def _ensure_index(self) -> _Index:
        if self._index is None:
            self._index = self._build_index()
        return self._index

    def _build_index(self) -> _Index:
        index = _Index(project=self._root.name)
        for path in self._python_files():
            rel = path.relative_to(self._root).as_posix()
            module_qname = self._module_qname(path)
            index.modules[rel] = module_qname
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
            except (SyntaxError, UnicodeDecodeError):
                continue
            _ModuleVisitor(index, module_qname, rel).visit(tree)
        return index

    def _python_files(self) -> list[Path]:
        files: list[Path] = []
        for path in self._root.rglob("*.py"):
            if any(part in _IGNORED_DIRS for part in path.relative_to(self._root).parts):
                continue
            files.append(path)
        return sorted(files)

    def _module_qname(self, path: Path) -> str:
        rel = path.relative_to(self._root)
        parts = list(rel.parts)
        parts[-1] = rel.stem
        if parts[-1] == "__init__":
            parts.pop()
        return ".".join([self._root.name, *parts])

    @staticmethod
    def _short_name(symbol: str) -> str:
        return symbol.rsplit(".", 1)[-1]

    def _to_symbol(self, definition: _Def, provenance: Provenance) -> Symbol:
        return Symbol(
            name=definition.qname,
            kind=definition.kind,
            location=definition.location,
            provenance=provenance,
            language="python",
            container=definition.container,
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
        short = self._short_name(name)
        provenance = Provenance(source=Source.AST, confidence=0.9)
        exact = index.defs_by_name.get(short, [])
        results = [self._to_symbol(d, provenance) for d in exact]
        if len(results) < limit:
            lowered = short.lower()
            for definition in index.defs:
                if definition in exact:
                    continue
                if lowered in definition.name.lower():
                    results.append(
                        self._to_symbol(
                            definition,
                            Provenance(source=Source.AST, confidence=0.6),
                        )
                    )
                if len(results) >= limit:
                    break
        return results[:limit]

    def repo_find_references(
        self, repo: RepoRef, symbol: str, *, include_definition: bool = True
    ) -> list[Reference]:
        index = self._ensure_index()
        short = self._short_name(symbol)
        provenance = Provenance(source=Source.AST, confidence=0.7)
        def_locations = {d.location for d in index.defs_by_name.get(short, [])}
        seen: set[Location] = set()
        references: list[Reference] = []
        if include_definition:
            for location in def_locations:
                seen.add(location)
                references.append(
                    Reference(
                        symbol=short,
                        location=location,
                        is_definition=True,
                        provenance=provenance,
                    )
                )
        for location in index.occurrences.get(short, []):
            if location in seen:
                continue
            seen.add(location)
            is_definition = location in def_locations
            if is_definition and not include_definition:
                continue
            references.append(
                Reference(
                    symbol=short,
                    location=location,
                    is_definition=is_definition,
                    provenance=provenance,
                )
            )
        return references

    def repo_find_implementations(self, repo: RepoRef, symbol: str) -> list[Symbol]:
        index = self._ensure_index()
        short = self._short_name(symbol)
        provenance = Provenance(source=Source.AST, confidence=0.8)
        return [
            self._to_symbol(definition, provenance)
            for definition in index.defs
            if definition.kind is SymbolKind.CLASS and short in definition.bases
        ]

    def repo_get_callers(self, repo: RepoRef, symbol: str) -> list[CallEdge]:
        index = self._ensure_index()
        short = self._short_name(symbol)
        provenance = Provenance(source=Source.AST, confidence=0.7)
        targets = index.defs_by_name.get(short, [])
        callee = (
            self._to_symbol(targets[0], provenance)
            if targets
            else self._unknown_symbol(short, None)
        )
        edges: list[CallEdge] = []
        for caller_qname, callee_name, call_site in index.calls:
            if callee_name != short:
                continue
            caller = self._caller_symbol(index, caller_qname, call_site)
            edges.append(
                CallEdge(
                    caller=caller,
                    callee=callee,
                    provenance=provenance,
                    call_site=call_site,
                )
            )
        return edges

    def repo_get_callees(self, repo: RepoRef, symbol: str) -> list[CallEdge]:
        index = self._ensure_index()
        provenance = Provenance(source=Source.AST, confidence=0.7)
        caller_qnames = {d.qname for d in index.defs_by_name.get(self._short_name(symbol), [])}
        edges: list[CallEdge] = []
        for caller_qname, callee_name, call_site in index.calls:
            if caller_qname is None or caller_qname not in caller_qnames:
                continue
            caller = self._caller_symbol(index, caller_qname, call_site)
            resolved = index.defs_by_name.get(callee_name, [])
            callee = (
                self._to_symbol(resolved[0], provenance)
                if resolved
                else self._unknown_symbol(callee_name, call_site)
            )
            edges.append(
                CallEdge(
                    caller=caller,
                    callee=callee,
                    provenance=provenance,
                    call_site=call_site,
                )
            )
        return edges

    def repo_get_related_tests(self, repo: RepoRef, target: str) -> list[RelatedTest]:
        index = self._ensure_index()
        short = self._short_name(target)
        provenance = Provenance(source=Source.HEURISTIC, confidence=0.6)
        results: list[RelatedTest] = []
        for rel, module_qname in index.modules.items():
            if not self._is_test_module(rel):
                continue
            if short in self._names_in_module(index, rel):
                results.append(
                    RelatedTest(
                        test_id=module_qname,
                        location=Location(path=rel, start_line=1),
                        covers=short,
                        provenance=provenance,
                    )
                )
        return results

    def repo_get_module_dependencies(self, repo: RepoRef, module: str) -> list[DependencyEdge]:
        index = self._ensure_index()
        provenance = Provenance(source=Source.AST, confidence=0.9)
        edges: list[DependencyEdge] = []
        for from_module, to_module, _location in index.imports:
            if module and from_module != module:
                continue
            edges.append(
                DependencyEdge(
                    from_module=from_module,
                    to_module=to_module,
                    kind="import",
                    provenance=provenance,
                )
            )
        return edges

    def repo_get_change_impact(self, repo: RepoRef, changed: list[str]) -> ChangeImpact:
        index = self._ensure_index()
        provenance = Provenance(source=Source.HEURISTIC, confidence=0.6)
        changed_modules = {self._resolve_module(index, entry) for entry in changed}
        changed_modules.discard("")
        impacted_files: set[str] = set()
        impacted_symbols: list[Symbol] = []
        impacted_tests: list[RelatedTest] = []
        symbol_provenance = Provenance(source=Source.HEURISTIC, confidence=0.6)
        for from_module, to_module, location in index.imports:
            if any(self._module_matches(to_module, target) for target in changed_modules):
                impacted_files.add(location.path)
                for definition in index.defs:
                    if definition.container and definition.container.startswith(from_module):
                        impacted_symbols.append(self._to_symbol(definition, symbol_provenance))
        for module in changed_modules:
            impacted_tests.extend(self.repo_get_related_tests(repo, module))
        return ChangeImpact(
            changed=tuple(changed),
            impacted_symbols=tuple(impacted_symbols),
            impacted_files=tuple(sorted(impacted_files)),
            impacted_tests=tuple(impacted_tests),
            provenance=provenance,
        )

    def repo_get_context_package(self, repo: RepoRef, focus: str) -> ContextPackage:
        symbols = self.repo_find_symbol(repo, focus)
        references = self.repo_find_references(repo, focus)
        related_tests = self.repo_get_related_tests(repo, focus)
        dependencies: list[DependencyEdge] = []
        if symbols:
            module = symbols[0].container or ""
            dependencies = self.repo_get_module_dependencies(repo, module)
        return ContextPackage(
            focus=focus,
            symbols=tuple(symbols),
            references=tuple(references),
            related_tests=tuple(related_tests),
            dependencies=tuple(dependencies),
        )

    def repo_get_index_status(self, repo: RepoRef) -> IndexStatus:
        index = self._ensure_index()
        detail = (
            f"{len(index.modules)} modules, {len(index.defs)} symbols, "
            f"{len(index.imports)} imports, {len(index.calls)} calls"
        )
        return IndexStatus(
            repo=repo,
            indexed_commit=repo.commit_sha or None,
            is_fresh=True,
            languages=("python",),
            detail=detail,
        )

    def _caller_symbol(
        self, index: _Index, caller_qname: str | None, call_site: Location
    ) -> Symbol:
        provenance = Provenance(source=Source.AST, confidence=0.7)
        if caller_qname is not None:
            definition = index.defs_by_qname.get(caller_qname)
            if definition is not None:
                return self._to_symbol(definition, provenance)
        module = index.modules.get(call_site.path, call_site.path)
        return Symbol(
            name=module,
            kind=SymbolKind.MODULE,
            location=Location(path=call_site.path, start_line=1),
            provenance=provenance,
            language="python",
        )

    def _unknown_symbol(self, name: str, call_site: Location | None) -> Symbol:
        location = call_site or Location(path="", start_line=0)
        return Symbol(
            name=name,
            kind=SymbolKind.UNKNOWN,
            location=location,
            provenance=Provenance(source=Source.HEURISTIC, confidence=0.4),
            language="python",
        )

    @staticmethod
    def _is_test_module(rel: str) -> bool:
        name = Path(rel).name
        parts = Path(rel).parts
        return (
            name.startswith("test_")
            or name.endswith("_test.py")
            or "tests" in parts
            or "test" in parts
        )

    def _names_in_module(self, index: _Index, rel: str) -> set[str]:
        names: set[str] = set()
        for name, locations in index.occurrences.items():
            if any(location.path == rel for location in locations):
                names.add(name)
        return names

    def _resolve_module(self, index: _Index, entry: str) -> str:
        if entry in index.modules:
            return index.modules[entry]
        candidate = Path(entry).as_posix()
        for rel, module_qname in index.modules.items():
            if rel == candidate:
                return module_qname
        return entry

    @staticmethod
    def _module_matches(imported: str, changed_module: str) -> bool:
        if not changed_module:
            return False
        tail = changed_module.split(".")[-1]
        return imported in (changed_module, tail) or imported.endswith(f".{tail}")
