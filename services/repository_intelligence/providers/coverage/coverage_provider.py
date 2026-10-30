"""Related-tests provider backed by coverage.py dynamic contexts.

Runs the target repository's test suite under coverage.py with
`dynamic_context = test_function`, then maps a target symbol's source line span
to the tests that execute it. Symbol spans and test locations are resolved with
stdlib `ast`, so the mapping does not depend on a SCIP index. Results carry
authoritative Source.COVERAGE provenance.
"""

from __future__ import annotations

import ast
import json
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from services import process
from services.repository_intelligence.normalization.models import (
    Location,
    Provenance,
    RelatedTest,
    RepoRef,
    Source,
)

_COVERAGE_CONFIDENCE = 1.0

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

_TEST_DIR_NAMES = frozenset({"tests", "test"})


class CoverageError(RuntimeError):
    """Raised when a coverage.py invocation fails."""


@dataclass(frozen=True)
class SymbolSpan:
    path: str
    start_line: int
    end_line: int


def _normalize_context(context: str) -> str | None:
    if not context:
        return None
    tail = context.split("|")[-1]
    return tail or None


def coverage_map_from_json(payload: dict) -> dict[str, dict[int, frozenset[str]]]:
    """Parse `coverage json` (show_contexts) into a path -> line -> tests map."""
    result: dict[str, dict[int, frozenset[str]]] = {}
    for path, file_data in payload.get("files", {}).items():
        contexts = file_data.get("contexts", {})
        line_map: dict[int, frozenset[str]] = {}
        for lineno, ctx_list in contexts.items():
            tests = frozenset(
                normalized
                for ctx in ctx_list
                if (normalized := _normalize_context(ctx)) is not None
            )
            if tests:
                line_map[int(lineno)] = tests
        if line_map:
            result[path] = line_map
    return result


def related_tests_from_coverage(
    coverage_map: dict[str, dict[int, frozenset[str]]],
    span: SymbolSpan,
    locate_test: Callable[[str], Location | None],
    *,
    covers: str,
    confidence: float = _COVERAGE_CONFIDENCE,
) -> list[RelatedTest]:
    """Map a symbol span to the tests covering it as deterministic RelatedTests."""
    line_map = coverage_map.get(span.path, {})
    tests: set[str] = set()
    for line in range(span.start_line, span.end_line + 1):
        tests |= set(line_map.get(line, ()))
    provenance = Provenance(source=Source.COVERAGE, confidence=confidence)
    results: list[RelatedTest] = []
    for test_id in tests:
        location = locate_test(test_id)
        if location is None:
            continue
        results.append(
            RelatedTest(
                test_id=test_id,
                location=location,
                covers=covers,
                provenance=provenance,
            )
        )
    results.sort(key=lambda test: (test.location.path, test.location.start_line, test.test_id))
    return results


def _split_module(repo_root: Path, qualified: str) -> tuple[Path | None, list[str]]:
    parts = qualified.split(".")
    for i in range(len(parts), 0, -1):
        prefix = parts[:i]
        module_file = repo_root.joinpath(*prefix[:-1], prefix[-1] + ".py")
        if module_file.is_file():
            return module_file, parts[i:]
        package_init = repo_root.joinpath(*prefix, "__init__.py")
        if package_init.is_file():
            return package_init, parts[i:]
    return None, []


def _find_node(tree: ast.Module, remainder: list[str]) -> ast.AST | None:
    node: ast.AST = tree
    for name in remainder:
        found = None
        for child in getattr(node, "body", []):
            if (
                isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
                and child.name == name
            ):
                found = child
                break
        if found is None:
            return None
        node = found
    return node


def resolve_symbol_span(repo_root: str | Path, qualified: str) -> SymbolSpan | None:
    """Resolve a qualified symbol to its source file and 1-based line span."""
    root = Path(repo_root)
    module_file, remainder = _split_module(root, qualified)
    if module_file is None or not remainder:
        return None
    try:
        tree = ast.parse(module_file.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return None
    node = _find_node(tree, remainder)
    if node is None:
        return None
    decorators = getattr(node, "decorator_list", [])
    start = min([node.lineno, *(decorator.lineno for decorator in decorators)])
    end = getattr(node, "end_lineno", None) or node.lineno
    return SymbolSpan(
        path=module_file.relative_to(root).as_posix(),
        start_line=start,
        end_line=end,
    )


def build_test_index(repo_root: str | Path) -> dict[str, Location]:
    """Index test-callable definitions keyed by `module.function` / `module.Class.method`."""
    root = Path(repo_root)
    index: dict[str, Location] = {}
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        if any(part in _IGNORED_DIRS for part in relative.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        module = path.stem
        rel = relative.as_posix()
        for node in tree.body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                index.setdefault(
                    f"{module}.{node.name}", Location(path=rel, start_line=node.lineno)
                )
            elif isinstance(node, ast.ClassDef):
                for member in node.body:
                    if isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef):
                        index.setdefault(
                            f"{module}.{node.name}.{member.name}",
                            Location(path=rel, start_line=member.lineno),
                        )
    return index


def _detect_source(repo_root: Path) -> str:
    packages = [
        child.name
        for child in sorted(repo_root.iterdir())
        if child.is_dir()
        and child.name not in _IGNORED_DIRS
        and child.name not in _TEST_DIR_NAMES
        and (child / "__init__.py").is_file()
    ]
    return ",".join(packages) if packages else repo_root.name


class CoverageProvider:
    """Answers repo_get_related_tests from coverage.py dynamic contexts."""

    def __init__(
        self,
        repo_root: str | Path,
        *,
        venv_python: str | Path | None = None,
        source: str | None = None,
        coverage_map: dict[str, dict[int, frozenset[str]]] | None = None,
    ) -> None:
        self._root = Path(repo_root).resolve()
        self._venv_python = str(venv_python) if venv_python is not None else None
        self._source = source
        self._coverage_map = coverage_map
        self._test_index: dict[str, Location] | None = None

    def repo_get_related_tests(self, repo: RepoRef, target: str) -> list[RelatedTest]:
        span = resolve_symbol_span(self._root, target)
        if span is None:
            return []
        coverage_map = self._ensure_coverage()
        return related_tests_from_coverage(coverage_map, span, self._locate_test, covers=target)

    def _ensure_coverage(self) -> dict[str, dict[int, frozenset[str]]]:
        if self._coverage_map is None:
            self._coverage_map = self._run_coverage()
        return self._coverage_map

    def _ensure_test_index(self) -> dict[str, Location]:
        if self._test_index is None:
            self._test_index = build_test_index(self._root)
        return self._test_index

    def _locate_test(self, test_id: str) -> Location | None:
        index = self._ensure_test_index()
        exact = index.get(test_id)
        if exact is not None:
            return exact
        suffix = f".{test_id}"
        matches = sorted(
            (qualified for qualified in index if qualified.endswith(suffix)),
        )
        return index[matches[0]] if matches else None

    def _resolve_python(self) -> str:
        if self._venv_python:
            return self._venv_python
        candidate = self._root / ".venv" / "bin" / "python"
        if candidate.is_file():
            return str(candidate)
        raise CoverageError("no interpreter for coverage run; pass venv_python")

    def _run_coverage(self) -> dict[str, dict[int, frozenset[str]]]:
        python = self._resolve_python()
        source = self._source or _detect_source(self._root)
        with tempfile.TemporaryDirectory() as tmp:
            rcfile = Path(tmp) / "coveragerc"
            data_file = Path(tmp) / "cov.data"
            json_out = Path(tmp) / "cov.json"
            rcfile.write_text(
                "\n".join(
                    [
                        "[run]",
                        f"source = {source}",
                        "dynamic_context = test_function",
                        "relative_files = True",
                        f"data_file = {data_file}",
                        "[json]",
                        "show_contexts = True",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            try:
                process.run(
                    [python, "-m", "coverage", "run", f"--rcfile={rcfile}", "-m", "pytest", "-q"],
                    timeout=process.LONG_COMMAND_TIMEOUT_SECONDS,
                    cwd=self._root,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                process.run(
                    [python, "-m", "coverage", "json", f"--rcfile={rcfile}", "-o", str(json_out)],
                    timeout=process.LONG_COMMAND_TIMEOUT_SECONDS,
                    cwd=self._root,
                    check=True,
                    capture_output=True,
                    text=True,
                )
            except process.CalledProcessError as error:
                raise CoverageError(
                    f"coverage run failed: {error.stderr.strip().splitlines()[-1:]}"
                ) from error
            payload = json.loads(json_out.read_text(encoding="utf-8"))
        return coverage_map_from_json(payload)
