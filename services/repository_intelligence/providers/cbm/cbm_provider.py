"""Repository-intelligence provider that drives codebase-memory-mcp in CLI mode."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
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

_SYMBOL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
_HEADER_RE = re.compile(r"^(\w+):\s+\d+\s+\(cols:\s*(.+?)\)\s*$")
_FILE_SUFFIX = ".py.__file__"

_KIND_BY_LABEL = {
    "Function": SymbolKind.FUNCTION,
    "Method": SymbolKind.METHOD,
    "Class": SymbolKind.CLASS,
    "Interface": SymbolKind.INTERFACE,
    "Enum": SymbolKind.ENUM,
    "Type": SymbolKind.TYPE,
    "Module": SymbolKind.MODULE,
    "Package": SymbolKind.PACKAGE,
    "Variable": SymbolKind.VARIABLE,
    "Route": SymbolKind.FUNCTION,
}

_SYMBOL_LABELS = (
    "Function",
    "Method",
    "Class",
    "Interface",
    "Enum",
    "Type",
    "Module",
    "Variable",
)


class CBMError(RuntimeError):
    pass


class CBMProvider:
    def __init__(
        self,
        repo_root: str | Path,
        binary: str | Path,
        *,
        allowed_root: str | Path | None = None,
        cache_dir: str | Path | None = None,
        project: str | None = None,
        timeout: int = 120,
    ) -> None:
        self._root = Path(repo_root).resolve()
        self._binary = str(Path(binary))
        self._allowed_root = Path(allowed_root).resolve() if allowed_root else self._root
        self._cache_dir = Path(cache_dir).resolve() if cache_dir else None
        self._timeout = timeout
        self._project = project
        self._indexed_commit: str | None = None
        if self._root != self._allowed_root and self._allowed_root not in self._root.parents:
            raise CBMError(
                f"repo_root {self._root} is outside CBM_ALLOWED_ROOT {self._allowed_root}"
            )

    def index(self) -> str:
        envelope = self._invoke("index_repository", repo_path=str(self._root))
        structured = self._structured(envelope)
        project = structured.get("project")
        if not isinstance(project, str):
            raise CBMError("index_repository did not return a project name")
        self._project = project
        self._indexed_commit = self._live_head(project)
        return project

    def _live_head(self, project: str) -> str | None:
        status = self._structured(self._invoke("index_status", project=project, verbose="true"))
        git = status.get("git", {}) if isinstance(status, dict) else {}
        head = git.get("head_sha") if isinstance(git, dict) else None
        return head if isinstance(head, str) and head else None

    def _ensure_project(self) -> str:
        if self._project is None:
            self.index()
        assert self._project is not None
        return self._project

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["CBM_ALLOWED_ROOT"] = str(self._allowed_root)
        if self._cache_dir is not None:
            env["CBM_CACHE_DIR"] = str(self._cache_dir)
        return env

    def _invoke(self, tool: str, **flags: object) -> dict:
        cmd = [self._binary, "cli", "--json", tool]
        for key, value in flags.items():
            if value is None:
                continue
            cmd.append("--" + key.replace("_", "-"))
            cmd.append(str(value))
        completed = subprocess.run(
            cmd,
            env=self._env(),
            capture_output=True,
            text=True,
            timeout=self._timeout,
        )
        if completed.returncode != 0:
            raise CBMError(
                f"{tool} exited {completed.returncode}: {completed.stderr.strip()[-500:]}"
            )
        try:
            envelope = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise CBMError(f"{tool} produced non-JSON output: {error}") from error
        if envelope.get("isError"):
            raise CBMError(f"{tool} reported an error: {envelope}")
        return envelope

    @staticmethod
    def _structured(envelope: dict) -> dict:
        structured = envelope.get("structuredContent")
        if isinstance(structured, dict) and "text" not in structured:
            return structured
        content = envelope.get("content") or []
        if content and isinstance(content[0], dict):
            text = content[0].get("text")
            if isinstance(text, str):
                return json.loads(text)
        raise CBMError("envelope has no structured content")

    @staticmethod
    def _table_text(envelope: dict) -> str:
        structured = envelope.get("structuredContent")
        if isinstance(structured, dict) and isinstance(structured.get("text"), str):
            return structured["text"]
        content = envelope.get("content") or []
        if content and isinstance(content[0], dict) and isinstance(content[0].get("text"), str):
            return content[0]["text"]
        raise CBMError("envelope has no table text")

    @staticmethod
    def _parse_table(text: str, section: str) -> list[dict[str, str]]:
        cols: list[str] | None = None
        collecting = False
        rows: list[dict[str, str]] = []
        for line in text.splitlines():
            header = _HEADER_RE.match(line.strip())
            if header is not None:
                collecting = header.group(1) == section
                cols = header.group(2).split() if collecting else cols
                continue
            if not collecting or cols is None:
                continue
            if not line.startswith("  "):
                collecting = False
                continue
            try:
                tokens = shlex.split(line)
            except ValueError:
                continue
            if tokens:
                rows.append(dict(zip(cols, tokens, strict=False)))
        return rows

    @staticmethod
    def _rows_from_json(structured: dict) -> list[dict[str, str]]:
        cols = structured.get("cols")
        rows = structured.get("rows")
        if not isinstance(cols, list) or not isinstance(rows, list):
            return []
        table: list[dict[str, str]] = []
        for row in rows:
            if isinstance(row, list):
                pairs = zip(cols, row, strict=False)
                table.append({str(c): "" if v is None else str(v) for c, v in pairs})
        return table

    def _query(self, cypher: str) -> list[dict[str, str]]:
        envelope = self._invoke("query_graph", project=self._ensure_project(), query=cypher)
        return self._parse_table(self._table_text(envelope), "rows")

    @staticmethod
    def _safe_symbol(symbol: str) -> str:
        candidate = symbol.strip()
        if not _SYMBOL_RE.match(candidate):
            raise CBMError(f"unsafe symbol expression: {symbol!r}")
        return candidate

    def _strip_project(self, qualified: str) -> str:
        prefix = f"{self._project}."
        return qualified[len(prefix) :] if qualified.startswith(prefix) else qualified

    def _target_where(self, var: str, symbol: str) -> str:
        safe = self._safe_symbol(symbol)
        project = self._ensure_project()
        clauses = [f"{var}.qualified_name ENDS WITH '.{safe}'"]
        if "." not in safe:
            clauses.append(f"{var}.name = '{safe}'")
        inner = " OR ".join(clauses)
        return f"(({inner}) AND {var}.qualified_name STARTS WITH '{project}.')"

    @staticmethod
    def _is_project_row(row: dict[str, str]) -> bool:
        file = row.get("file", "")
        return bool(file) and not file.startswith("<")

    @staticmethod
    def _int(value: str | None) -> int:
        try:
            return int(value) if value not in (None, "-", "") else 0
        except (TypeError, ValueError):
            return 0

    def _location(self, row: dict[str, str]) -> Location:
        return Location(
            path=row.get("file", ""),
            start_line=self._int(row.get("line")),
            end_line=self._int(row.get("end")) or None,
        )

    def _symbol_from_row(self, row: dict[str, str], confidence: float) -> Symbol:
        qualified = self._strip_project(row.get("qn", ""))
        kind = _KIND_BY_LABEL.get(row.get("label", ""), SymbolKind.UNKNOWN)
        container = qualified.rsplit(".", 1)[0] if "." in qualified else None
        return Symbol(
            name=qualified,
            kind=kind,
            location=self._location(row),
            provenance=Provenance(source=Source.CBM, confidence=confidence),
            language="python",
            container=container,
        )

    def repo_search_code(self, repo: RepoRef, query: str, *, limit: int = 50) -> list[SearchHit]:
        envelope = self._invoke(
            "search_graph", project=self._ensure_project(), query=query, format="json", limit=limit
        )
        rows = self._rows_from_json(self._structured(envelope))
        hits: list[SearchHit] = []
        total = len(rows) or 1
        for rank, row in enumerate(rows):
            if not self._is_project_row(row):
                continue
            start = self._int(row.get("lines", "").split("-")[0])
            hits.append(
                SearchHit(
                    location=Location(path=row.get("file", ""), start_line=start),
                    snippet=self._strip_project(row.get("qn", "")),
                    score=(total - rank) / total,
                    provenance=Provenance(source=Source.CBM, confidence=1.0),
                )
            )
        return hits

    def repo_find_symbol(self, repo: RepoRef, name: str, *, limit: int = 50) -> list[Symbol]:
        short = self._safe_symbol(name).rsplit(".", 1)[-1]
        labels = ", ".join(f"'{label}'" for label in _SYMBOL_LABELS)
        rows = self._query(
            f"MATCH (n) WHERE n.name = '{short}' AND n.label IN [{labels}] "
            "RETURN n.qualified_name AS qn, n.label AS label, "
            f"n.file_path AS file, n.start_line AS line, n.end_line AS end LIMIT {int(limit)}"
        )
        return [self._symbol_from_row(row, 0.9) for row in rows if self._is_project_row(row)]

    def repo_find_references(
        self, repo: RepoRef, symbol: str, *, include_definition: bool = True
    ) -> list[Reference]:
        short = self._safe_symbol(symbol).rsplit(".", 1)[-1]
        provenance = Provenance(source=Source.CBM, confidence=0.7)
        references: list[Reference] = []
        if include_definition:
            for row in self.repo_find_symbol(repo, symbol):
                references.append(
                    Reference(
                        symbol=short,
                        location=row.location,
                        is_definition=True,
                        provenance=provenance,
                    )
                )
        rows = self._query(
            f"MATCH (s)-[:USAGE|CALLS|USES_TYPE]->(t) WHERE {self._target_where('t', symbol)} "
            "RETURN s.qualified_name AS qn, s.file_path AS file, s.start_line AS line"
        )
        for row in rows:
            if not self._is_project_row(row):
                continue
            references.append(
                Reference(
                    symbol=short,
                    location=self._location(row),
                    is_definition=False,
                    provenance=provenance,
                )
            )
        return references

    def repo_find_implementations(self, repo: RepoRef, symbol: str) -> list[Symbol]:
        short = self._safe_symbol(symbol).rsplit(".", 1)[-1]
        rows = self._query(
            "MATCH (c:Class) WHERE c.base_classes IS NOT NULL "
            "RETURN c.qualified_name AS qn, c.label AS label, "
            "c.file_path AS file, c.start_line AS line, c.base_classes AS bases"
        )
        results: list[Symbol] = []
        for row in rows:
            if not self._is_project_row(row):
                continue
            try:
                bases = json.loads(row.get("bases", "[]"))
            except (json.JSONDecodeError, TypeError):
                continue
            if short in bases:
                results.append(self._symbol_from_row(row, 0.9))
        return results

    def repo_get_callers(self, repo: RepoRef, symbol: str) -> list[CallEdge]:
        rows = self._query(
            f"MATCH (c)-[:CALLS]->(t) WHERE {self._target_where('t', symbol)} "
            "RETURN c.qualified_name AS qn, c.label AS label, "
            "c.file_path AS file, c.start_line AS line, "
            "t.qualified_name AS target_qn, t.label AS target_label, "
            "t.file_path AS target_file, t.start_line AS target_line"
        )
        return [
            self._call_edge(row, caller_is_source=True) for row in rows if self._is_project_row(row)
        ]

    def repo_get_callees(self, repo: RepoRef, symbol: str) -> list[CallEdge]:
        rows = self._query(
            f"MATCH (s)-[:CALLS]->(c) WHERE {self._target_where('s', symbol)} "
            "RETURN c.qualified_name AS qn, c.label AS label, "
            "c.file_path AS file, c.start_line AS line, "
            "s.qualified_name AS target_qn, s.label AS target_label, "
            "s.file_path AS target_file, s.start_line AS target_line"
        )
        return [
            self._call_edge(row, caller_is_source=False)
            for row in rows
            if self._is_project_row(row)
        ]

    def _call_edge(self, row: dict[str, str], *, caller_is_source: bool) -> CallEdge:
        provenance = Provenance(source=Source.CBM, confidence=0.8)
        node = self._symbol_from_row(row, 0.8)
        other = self._symbol_from_row(
            {
                "qn": row.get("target_qn", ""),
                "label": row.get("target_label", ""),
                "file": row.get("target_file", ""),
                "line": row.get("target_line", ""),
            },
            0.8,
        )
        caller, callee = (node, other) if caller_is_source else (other, node)
        return CallEdge(
            caller=caller,
            callee=callee,
            provenance=provenance,
            call_site=caller.location,
        )

    def repo_get_related_tests(self, repo: RepoRef, target: str) -> list[RelatedTest]:
        short = self._safe_symbol(target).rsplit(".", 1)[-1]
        rows = self._query(
            f"MATCH (test)-[:TESTS]->(t) WHERE {self._target_where('t', target)} "
            "RETURN test.qualified_name AS qn, test.file_path AS file, test.start_line AS line"
        )
        results: list[RelatedTest] = []
        for row in rows:
            if not self._is_project_row(row):
                continue
            results.append(
                RelatedTest(
                    test_id=self._strip_project(row.get("qn", "")),
                    location=self._location(row),
                    covers=short,
                    provenance=Provenance(source=Source.CBM, confidence=0.7),
                )
            )
        return results

    def repo_get_module_dependencies(self, repo: RepoRef, module: str) -> list[DependencyEdge]:
        safe = self._safe_symbol(module)
        rows = self._query(
            f"MATCH (f)-[:IMPORTS]->(m) WHERE f.qualified_name ENDS WITH '{safe}{_FILE_SUFFIX}' "
            "RETURN m.qualified_name AS qn, m.label AS label, m.file_path AS file"
        )
        provenance = Provenance(source=Source.CBM, confidence=0.9)
        edges: list[DependencyEdge] = []
        seen: set[str] = set()
        for row in rows:
            to_module = self._strip_project(row.get("qn", ""))
            if not to_module or to_module in seen:
                continue
            seen.add(to_module)
            edges.append(
                DependencyEdge(
                    from_module=safe,
                    to_module=to_module,
                    kind="import",
                    provenance=provenance,
                )
            )
        return edges

    def repo_get_change_impact(self, repo: RepoRef, changed: list[str]) -> ChangeImpact:
        provenance = Provenance(source=Source.CBM, confidence=0.7)
        impacted_symbols: list[Symbol] = []
        impacted_files: set[str] = set()
        impacted_tests: list[RelatedTest] = []
        seen: set[str] = set()
        for entry in changed:
            rows = self._query(
                f"MATCH (d)-[:CALLS|IMPORTS|USAGE|USES_TYPE]->(t) "
                f"WHERE {self._target_where('t', entry)} "
                "RETURN d.qualified_name AS qn, d.label AS label, "
                "d.file_path AS file, d.start_line AS line"
            )
            for row in rows:
                if not self._is_project_row(row):
                    continue
                qualified = row.get("qn", "")
                impacted_files.add(row.get("file", ""))
                if qualified not in seen:
                    seen.add(qualified)
                    impacted_symbols.append(self._symbol_from_row(row, 0.7))
            impacted_tests.extend(self.repo_get_related_tests(repo, entry))
        return ChangeImpact(
            changed=tuple(changed),
            impacted_symbols=tuple(impacted_symbols),
            impacted_files=tuple(sorted(f for f in impacted_files if f)),
            impacted_tests=tuple(impacted_tests),
            provenance=provenance,
        )

    def repo_get_context_package(self, repo: RepoRef, focus: str) -> ContextPackage:
        symbols = self.repo_find_symbol(repo, focus)
        references = self.repo_find_references(repo, focus)
        related_tests = self.repo_get_related_tests(repo, focus)
        dependencies: list[DependencyEdge] = []
        if symbols and symbols[0].container:
            dependencies = self.repo_get_module_dependencies(repo, symbols[0].container)
        return ContextPackage(
            focus=focus,
            symbols=tuple(symbols),
            references=tuple(references),
            related_tests=tuple(related_tests),
            dependencies=tuple(dependencies),
        )

    def repo_get_index_status(self, repo: RepoRef) -> IndexStatus:
        envelope = self._invoke("index_status", project=self._ensure_project(), verbose="true")
        structured = self._structured(envelope)
        git = structured.get("git", {}) if isinstance(structured, dict) else {}
        current_head = git.get("head_sha") if isinstance(git, dict) else None
        is_fresh = (
            self._indexed_commit is not None
            and current_head is not None
            and self._indexed_commit == current_head
        )
        detail = f"{structured.get('nodes')} nodes, {structured.get('edges')} edges"
        return IndexStatus(
            repo=repo,
            indexed_commit=self._indexed_commit,
            is_fresh=is_fresh,
            languages=("python",),
            detail=detail,
        )
