"""coverage.py oracle: map source lines to the tests that execute them."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from services import process


def _normalize_context(context: str) -> str | None:
    if not context:
        return None
    tail = context.split("|")[-1]
    return tail or None


def run_coverage(
    repo_root: str | Path,
    venv_python: str | Path,
    source: str = "app",
) -> dict[str, dict[int, set[str]]]:
    root = Path(repo_root)
    python = str(venv_python)
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
        process.run(
            [python, "-m", "coverage", "run", f"--rcfile={rcfile}", "-m", "pytest", "-q"],
            timeout=process.LONG_COMMAND_TIMEOUT_SECONDS,
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
        process.run(
            [python, "-m", "coverage", "json", f"--rcfile={rcfile}", "-o", str(json_out)],
            timeout=process.LONG_COMMAND_TIMEOUT_SECONDS,
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(json_out.read_text(encoding="utf-8"))

    result: dict[str, dict[int, set[str]]] = {}
    for path, file_data in payload.get("files", {}).items():
        contexts = file_data.get("contexts", {})
        line_map: dict[int, set[str]] = {}
        for lineno, ctx_list in contexts.items():
            tests = {
                normalized
                for ctx in ctx_list
                if (normalized := _normalize_context(ctx)) is not None
            }
            if tests:
                line_map[int(lineno)] = tests
        if line_map:
            result[path] = line_map
    return result


def coverage_version(venv_python: str | Path) -> str:
    completed = process.run(
        [str(venv_python), "-c", "import coverage; print(coverage.__version__)"],
        timeout=process.SHORT_COMMAND_TIMEOUT_SECONDS,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()
