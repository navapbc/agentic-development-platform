"""Parse the JSON emitted by `scip print --json` into normalized code facts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

ROLE_DEFINITION = 0x1
ROLE_IMPORT = 0x2
ROLE_WRITE = 0x4
ROLE_READ = 0x8

_MODULE_SUFFIX = "/__init__:"
_SUFFIX_KIND = {"/": "namespace", "#": "type", ".": "term", ":": "meta"}
_NAME_STOP = "#/.:()[]`"
_PATH_KINDS = ("type", "term", "meta", "method")


@dataclass(frozen=True)
class SymbolInfo:
    symbol: str
    is_project: bool
    kind: str
    module: str | None = None
    qualified: str | None = None
    name: str | None = None
    relationships: tuple[tuple[str, bool], ...] = ()


@dataclass(frozen=True)
class Occurrence:
    path: str
    symbol: str
    start_line: int
    start_col: int
    end_line: int
    end_col: int
    roles: int
    enclosing: tuple[int, int, int, int] | None = None

    @property
    def is_definition(self) -> bool:
        return bool(self.roles & ROLE_DEFINITION)


def _to_range(raw: list[int]) -> tuple[int, int, int, int]:
    if len(raw) == 3:
        start_line, start_col, end_col = raw
        end_line = start_line
    else:
        start_line, start_col, end_line, end_col = raw
    return (start_line + 1, start_col, end_line + 1, end_col)


def _read_name(descriptor: str, start: int) -> tuple[str, int]:
    if descriptor[start] == "`":
        chars: list[str] = []
        i = start + 1
        while i < len(descriptor):
            if descriptor[i] == "`":
                if i + 1 < len(descriptor) and descriptor[i + 1] == "`":
                    chars.append("`")
                    i += 2
                    continue
                return "".join(chars), i + 1
            chars.append(descriptor[i])
            i += 1
        return "".join(chars), i
    i = start
    while i < len(descriptor) and descriptor[i] not in _NAME_STOP:
        i += 1
    return descriptor[start:i], i


def _parse_descriptors(descriptor: str) -> list[tuple[str, str]]:
    parts: list[tuple[str, str]] = []
    i = 0
    length = len(descriptor)
    while i < length:
        char = descriptor[i]
        if char == "(":
            close = descriptor.find(")", i + 1)
            if close == -1:
                break
            parts.append((descriptor[i + 1 : close], "parameter"))
            i = close + 1
            continue
        if char == "[":
            close = descriptor.find("]", i + 1)
            if close == -1:
                break
            parts.append((descriptor[i + 1 : close], "type_parameter"))
            i = close + 1
            continue
        name, i = _read_name(descriptor, i)
        if i < length and descriptor[i] == "(":
            close = descriptor.find(")", i + 1)
            if close == -1:
                break
            parts.append((name, "method"))
            i = close + 1
            if i < length and descriptor[i] == ".":
                i += 1
            continue
        if i < length and descriptor[i] in _SUFFIX_KIND:
            parts.append((name, _SUFFIX_KIND[descriptor[i]]))
            i += 1
            continue
        break
    return parts


def parse_symbol(symbol: str, project: str) -> SymbolInfo:
    if symbol.startswith("local "):
        return SymbolInfo(symbol=symbol, is_project=False, kind="local")
    parts = symbol.split(" ", 4)
    if len(parts) < 5:
        return SymbolInfo(symbol=symbol, is_project=False, kind="unknown")
    _scheme, _manager, package, _version, descriptor = parts
    is_project = package == project

    descriptors = _parse_descriptors(descriptor)
    namespaces = [name for name, kind in descriptors if kind == "namespace"]
    module = "/".join(namespaces) if namespaces else None
    path = [(name, kind) for name, kind in descriptors if kind in _PATH_KINDS]
    if module is not None and (descriptor.endswith("/") or path == [("__init__", "meta")]):
        return SymbolInfo(
            symbol=symbol,
            is_project=is_project,
            kind="module",
            module=module,
            qualified=module,
            name=module.rsplit(".", 1)[-1] if module else None,
        )

    if not path:
        return SymbolInfo(symbol=symbol, is_project=is_project, kind="unknown", module=module)

    names = [name for name, _ in path]
    last_kind = descriptors[-1][1]
    if last_kind == "parameter":
        kind = "parameter"
    elif last_kind == "type_parameter":
        kind = "type_parameter"
    elif last_kind == "type":
        kind = "class"
    elif last_kind == "method":
        kind = "function" if len(names) <= 1 else "method"
    elif last_kind == "term":
        kind = "variable" if len(names) <= 1 else "field"
    else:
        kind = last_kind

    qualified = f"{module}.{'.'.join(names)}" if module else ".".join(names)
    return SymbolInfo(
        symbol=symbol,
        is_project=is_project,
        kind=kind,
        module=module,
        qualified=qualified,
        name=names[-1],
    )


@dataclass
class ScipIndex:
    project: str
    occurrences: list[Occurrence] = field(default_factory=list)
    symbols: dict[str, SymbolInfo] = field(default_factory=dict)
    documents: list[str] = field(default_factory=list)
    _definition: dict[str, Occurrence] = field(default_factory=dict)
    _references: dict[str, list[Occurrence]] = field(default_factory=dict)
    _enclosing: dict[str, list[tuple[str, tuple[int, int, int, int]]]] = field(default_factory=dict)
    _path_module: dict[str, str] = field(default_factory=dict)
    _project_modules: set[str] = field(default_factory=set)

    @classmethod
    def from_scip_json(cls, data: dict[str, Any], project: str) -> ScipIndex:
        index = cls(project=project)
        for document in data.get("documents", []):
            path = document.get("relative_path", "")
            index.documents.append(path)
            for entry in document.get("symbols", []):
                info = parse_symbol(entry["symbol"], project)
                relationships = tuple(
                    (rel["symbol"], bool(rel.get("is_implementation")))
                    for rel in entry.get("relationships", [])
                )
                if relationships:
                    info = SymbolInfo(
                        symbol=info.symbol,
                        is_project=info.is_project,
                        kind=info.kind,
                        module=info.module,
                        qualified=info.qualified,
                        name=info.name,
                        relationships=relationships,
                    )
                index.symbols[entry["symbol"]] = info
            for raw in document.get("occurrences", []):
                start_line, start_col, end_line, end_col = _to_range(raw["range"])
                enclosing = raw.get("enclosing_range")
                occ = Occurrence(
                    path=path,
                    symbol=raw["symbol"],
                    start_line=start_line,
                    start_col=start_col,
                    end_line=end_line,
                    end_col=end_col,
                    roles=raw.get("symbol_roles", 0),
                    enclosing=_to_range(enclosing) if enclosing else None,
                )
                index.occurrences.append(occ)
        index._index_occurrences()
        return index

    def _index_occurrences(self) -> None:
        for occ in self.occurrences:
            if occ.symbol not in self.symbols:
                self.symbols[occ.symbol] = parse_symbol(occ.symbol, self.project)
            info = self.symbols[occ.symbol]
            if occ.is_definition:
                self._definition.setdefault(occ.symbol, occ)
                if info.kind == "module" and info.module:
                    self._path_module[occ.path] = info.module
                    self._project_modules.add(info.module)
                if (
                    occ.enclosing is not None
                    and info.is_project
                    and info.kind in {"function", "method", "class"}
                ):
                    self._enclosing.setdefault(occ.path, []).append((occ.symbol, occ.enclosing))
            else:
                self._references.setdefault(occ.symbol, []).append(occ)

    def project_symbols(self, kinds: set[str]) -> list[SymbolInfo]:
        seen: dict[str, SymbolInfo] = {}
        for info in self.symbols.values():
            if info.is_project and info.kind in kinds and info.symbol in self._definition:
                seen[info.symbol] = info
        return sorted(seen.values(), key=lambda info: info.qualified or info.symbol)

    def definition_of(self, symbol: str) -> Occurrence | None:
        return self._definition.get(symbol)

    def references_of(self, symbol: str) -> list[Occurrence]:
        return list(self._references.get(symbol, []))

    def callers_of(self, symbol: str) -> list[tuple[str | None, Occurrence]]:
        callers: list[tuple[str | None, Occurrence]] = []
        for occ in self.references_of(symbol):
            enclosing = self._enclosing_symbol(occ)
            callers.append((enclosing, occ))
        return callers

    def _enclosing_symbol(self, occ: Occurrence) -> str | None:
        best: str | None = None
        best_span = None
        for symbol, (sl, sc, el, ec) in self._enclosing.get(occ.path, []):
            if symbol == occ.symbol:
                continue
            if (sl, sc) <= (occ.start_line, occ.start_col) and (
                occ.start_line,
                occ.start_col,
            ) <= (el, ec):
                span = (el - sl, ec - sc)
                if best_span is None or span < best_span:
                    best = symbol
                    best_span = span
        return best

    def implementations_of(self, base_symbol: str) -> list[SymbolInfo]:
        results = []
        for info in self.symbols.values():
            if not info.is_project:
                continue
            if any(target == base_symbol and is_impl for target, is_impl in info.relationships):
                results.append(info)
        return results

    def base_symbols(self) -> dict[str, list[SymbolInfo]]:
        bases: dict[str, list[SymbolInfo]] = {}
        for info in self.symbols.values():
            if not info.is_project:
                continue
            for target, is_impl in info.relationships:
                if is_impl:
                    bases.setdefault(target, []).append(info)
        return bases

    def module_dependencies(self) -> dict[str, list[tuple[str, bool]]]:
        deps: dict[str, set[tuple[str, bool]]] = {}
        for occ in self.occurrences:
            if not occ.symbol.endswith(_MODULE_SUFFIX):
                continue
            target = self.symbols[occ.symbol]
            if target.module is None:
                continue
            owner = self._path_module.get(occ.path)
            if owner is None or owner == target.module:
                continue
            is_external = target.module not in self._project_modules
            deps.setdefault(owner, set()).add((target.module, is_external))
        return {module: sorted(edges) for module, edges in deps.items()}
