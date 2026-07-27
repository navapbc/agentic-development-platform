"""Generate the labeled gold set for a repository from independent oracles."""

from __future__ import annotations

import argparse
import json
import platform
import shlex
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from services.repository_intelligence.eval.coverage_oracle import (
    coverage_version,
    run_coverage,
)
from services.repository_intelligence.eval.gold_schema import (
    ExpectedItem,
    GoldMeta,
    GoldRecord,
    QueryType,
    write_gold_set,
)
from services.repository_intelligence.eval.scip_oracle import (
    ScipIndex,
    SymbolInfo,
    parse_symbol,
)

_DEFINITION_KINDS = {"class", "function", "method", "variable"}
_CALLABLE_KINDS = {"class", "function", "method"}
_CALLER_TARGET_KINDS = {"function", "method"}


def _resolve_commit(repo_root: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _generate_scip_index(
    repo_root: Path, project: str, version: str, scip_python_cmd: str, output: Path
) -> Path:
    subprocess.run(
        [
            *shlex.split(scip_python_cmd),
            "index",
            "--project-name",
            project,
            "--project-version",
            version,
            "--output",
            str(output),
            "--quiet",
        ],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return output


def _read_scip_json(scip_cli: str, index_path: Path) -> dict:
    completed = subprocess.run(
        [scip_cli, "print", "--json", str(index_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def _def_item(index: ScipIndex, info: SymbolInfo) -> ExpectedItem | None:
    occ = index.definition_of(info.symbol)
    if occ is None:
        return None
    return ExpectedItem(
        path=occ.path, line=occ.start_line, col=occ.start_col, symbol=info.qualified
    )


def _qualified_locations(index: ScipIndex) -> dict[str, tuple[str, int]]:
    locations: dict[str, tuple[str, int]] = {}
    for info in index.project_symbols(_CALLABLE_KINDS):
        occ = index.definition_of(info.symbol)
        if occ is not None and info.qualified:
            locations[info.qualified] = (occ.path, occ.start_line)
    return locations


def _symbol_definition_records(index: ScipIndex, repo: str, commit: str) -> list[GoldRecord]:
    by_name: dict[str, list[SymbolInfo]] = {}
    for info in index.project_symbols(_DEFINITION_KINDS):
        if info.name:
            by_name.setdefault(info.name, []).append(info)
    records: list[GoldRecord] = []
    for name, infos in sorted(by_name.items()):
        expected = tuple(item for info in infos if (item := _def_item(index, info)) is not None)
        records.append(
            GoldRecord(
                repo=repo,
                commit=commit,
                query_type=QueryType.SYMBOL_DEFINITION,
                input={"name": name},
                expected=expected,
                oracle="scip-python",
            )
        )
    return records


def _references_records(index: ScipIndex, repo: str, commit: str) -> list[GoldRecord]:
    records: list[GoldRecord] = []
    for info in index.project_symbols(_DEFINITION_KINDS):
        expected = tuple(
            ExpectedItem(
                path=occ.path,
                line=occ.start_line,
                col=occ.start_col,
                symbol=info.qualified,
            )
            for occ in index.references_of(info.symbol)
        )
        records.append(
            GoldRecord(
                repo=repo,
                commit=commit,
                query_type=QueryType.REFERENCES,
                input={"symbol": info.qualified, "name": info.name},
                expected=expected,
                oracle="scip-python",
            )
        )
    return records


def _callers_records(index: ScipIndex, repo: str, commit: str) -> list[GoldRecord]:
    records: list[GoldRecord] = []
    for info in index.project_symbols(_CALLER_TARGET_KINDS):
        seen: set[tuple] = set()
        items: list[ExpectedItem] = []
        for enclosing_symbol, occ in index.callers_of(info.symbol):
            if enclosing_symbol is None:
                key = ("module", occ.path, occ.start_line)
                if key in seen:
                    continue
                seen.add(key)
                items.append(ExpectedItem(path=occ.path, line=occ.start_line, module_level=True))
                continue
            definition = index.definition_of(enclosing_symbol)
            if definition is None:
                continue
            key = ("def", enclosing_symbol)
            if key in seen:
                continue
            seen.add(key)
            enclosing_info = index.symbols.get(enclosing_symbol)
            items.append(
                ExpectedItem(
                    path=definition.path,
                    line=definition.start_line,
                    col=definition.start_col,
                    symbol=enclosing_info.qualified if enclosing_info else None,
                )
            )
        records.append(
            GoldRecord(
                repo=repo,
                commit=commit,
                query_type=QueryType.CALLERS,
                input={"symbol": info.qualified, "name": info.name},
                expected=tuple(items),
                oracle="scip-python",
            )
        )
    return records


def _implementations_records(index: ScipIndex, repo: str, commit: str) -> list[GoldRecord]:
    records: list[GoldRecord] = []
    for base_symbol, impls in sorted(index.base_symbols().items()):
        base_info = index.symbols.get(base_symbol) or parse_symbol(base_symbol, index.project)
        expected = tuple(item for info in impls if (item := _def_item(index, info)) is not None)
        records.append(
            GoldRecord(
                repo=repo,
                commit=commit,
                query_type=QueryType.IMPLEMENTATIONS,
                input={"name": base_info.name, "base": base_info.qualified},
                expected=expected,
                oracle="scip-python",
            )
        )
    return records


def _module_dependencies_records(index: ScipIndex, repo: str, commit: str) -> list[GoldRecord]:
    records: list[GoldRecord] = []
    for module, edges in sorted(index.module_dependencies().items()):
        expected = tuple(
            ExpectedItem(module=dep, is_external=is_external) for dep, is_external in edges
        )
        records.append(
            GoldRecord(
                repo=repo,
                commit=commit,
                query_type=QueryType.MODULE_DEPENDENCIES,
                input={"module": module},
                expected=expected,
                oracle="scip-python",
            )
        )
    return records


def _related_tests_records(
    index: ScipIndex,
    repo: str,
    commit: str,
    coverage_map: dict[str, dict[int, set[str]]],
    locations: dict[str, tuple[str, int]],
) -> list[GoldRecord]:
    records: list[GoldRecord] = []
    for info in index.project_symbols(_CALLABLE_KINDS):
        occ = index.definition_of(info.symbol)
        if occ is None:
            continue
        start, end = (
            (occ.enclosing[0], occ.enclosing[2])
            if occ.enclosing is not None
            else (occ.start_line, occ.start_line)
        )
        line_map = coverage_map.get(occ.path, {})
        tests: set[str] = set()
        for line in range(start, end + 1):
            tests |= line_map.get(line, set())
        if not tests:
            continue
        items = []
        for test_id in sorted(tests):
            location = _resolve_test_location(test_id, locations)
            items.append(
                ExpectedItem(
                    test_id=test_id,
                    path=location[0] if location else None,
                    line=location[1] if location else None,
                )
            )
        records.append(
            GoldRecord(
                repo=repo,
                commit=commit,
                query_type=QueryType.RELATED_TESTS,
                input={"symbol": info.qualified, "name": info.name},
                expected=tuple(items),
                oracle="coverage.py",
            )
        )
    return records


def _resolve_test_location(
    test_id: str, locations: dict[str, tuple[str, int]]
) -> tuple[str, int] | None:
    exact = locations.get(test_id)
    if exact is not None:
        return exact
    suffix = "." + test_id
    matches = sorted(
        location for qualified, location in locations.items() if qualified.endswith(suffix)
    )
    return matches[0] if matches else None


def _tool_version(command: list[str]) -> str:
    try:
        completed = subprocess.run(command, check=True, capture_output=True, text=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"
    return (completed.stdout or completed.stderr).strip().splitlines()[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--out")
    parser.add_argument("--scip-json")
    parser.add_argument("--scip-index")
    parser.add_argument("--scip-python-cmd", default="npx --yes @sourcegraph/scip-python@0.6.6")
    parser.add_argument("--scip-python-version", default="0.6.6")
    parser.add_argument("--scip-cli", default="scip")
    parser.add_argument("--venv-python")
    parser.add_argument("--no-coverage", action="store_true")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    project = repo_root.name
    commit = _resolve_commit(repo_root)

    if args.scip_json:
        data = json.loads(Path(args.scip_json).read_text(encoding="utf-8"))
    else:
        index_path = (
            Path(args.scip_index)
            if args.scip_index
            else _generate_scip_index(
                repo_root,
                project,
                commit[:7],
                args.scip_python_cmd,
                repo_root / "index.scip",
            )
        )
        data = _read_scip_json(args.scip_cli, index_path)

    index = ScipIndex.from_scip_json(data, project)
    locations = _qualified_locations(index)

    coverage_map: dict[str, dict[int, set[str]]] = {}
    coverage_note = None
    venv_python = args.venv_python or (repo_root / ".venv" / "bin" / "python")
    if not args.no_coverage and Path(venv_python).exists():
        try:
            coverage_map = run_coverage(repo_root, venv_python)
        except subprocess.CalledProcessError as error:
            coverage_note = f"coverage failed: {error.stderr.strip().splitlines()[-1:]}"
    elif not args.no_coverage:
        coverage_note = "coverage skipped: no venv python found"

    records: list[GoldRecord] = []
    records += _symbol_definition_records(index, project, commit)
    records += _references_records(index, project, commit)
    records += _callers_records(index, project, commit)
    records += _implementations_records(index, project, commit)
    records += _module_dependencies_records(index, project, commit)
    records += _related_tests_records(index, project, commit, coverage_map, locations)

    oracles = {
        "structure": f"scip-python@{args.scip_python_version}",
        "scip_reader": _tool_version([args.scip_cli, "--version"]),
    }
    if coverage_map:
        oracles["related_tests"] = f"coverage.py@{coverage_version(venv_python)}"

    notes = [
        "callers gold covers function and method targets only; class instantiation is not modeled",
        "callers recall is bounded by scip-python; builtin-shadowing method calls may be missed",
    ]
    if coverage_note:
        notes.append(coverage_note)

    meta = GoldMeta(
        repo=project,
        commit=commit,
        generated_at=datetime.now(UTC).isoformat(),
        oracles=oracles,
        toolchain={
            "python": platform.python_version(),
            "node": _tool_version(["node", "--version"]),
        },
        notes="; ".join(notes),
    )

    out_path = (
        Path(args.out)
        if args.out
        else Path(__file__).parent / "gold" / f"{project}@{commit[:7]}.jsonl"
    )
    write_gold_set(out_path, meta, records)

    by_type: dict[str, int] = {}
    for record in records:
        by_type[str(record.query_type)] = by_type.get(str(record.query_type), 0) + 1
    print(f"wrote {len(records)} records to {out_path}")
    for query_type, count in sorted(by_type.items()):
        print(f"  {query_type}: {count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
