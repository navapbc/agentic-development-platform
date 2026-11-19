"""Invariants that keep developer machines and operator configuration out of tracked files."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

DEVELOPER_MACHINE_PATH_MARKERS = ("/Users/", "Documents/Projects")

THIS_FILE = "tests/unit/test_repo_hygiene.py"


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout


def _files_containing(markers: tuple[str, ...]) -> list[str]:
    hits = []
    for name in _git("ls-files").splitlines():
        path = REPO_ROOT / name
        if name == THIS_FILE or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError, OSError:
            continue
        if any(marker in text for marker in markers):
            hits.append(name)
    return sorted(hits)


def test_no_tracked_file_carries_a_developer_machine_path():
    assert _files_containing(DEVELOPER_MACHINE_PATH_MARKERS) == []


@pytest.mark.parametrize(
    "scorecard",
    sorted(
        p.name
        for p in (REPO_ROOT / "services/repository_intelligence/eval/scorecards").glob("*.json")
    ),
)
def test_scorecard_provenance_paths_are_repository_relative(scorecard):
    card = json.loads(
        (REPO_ROOT / "services/repository_intelligence/eval/scorecards" / scorecard).read_text(
            encoding="utf-8"
        )
    )
    recorded = [card.get("gold", {}).get("path"), card.get("toolchain", {}).get("cbm_binary")]

    assert [p for p in recorded if p and p.startswith("/")] == []


@pytest.mark.parametrize(
    "example", sorted(p.name for p in (REPO_ROOT / "config" / "local").glob("*.yaml.example"))
)
def test_every_operator_config_is_ignored_and_has_an_example(example):
    operator_file = f"config/local/{example.removesuffix('.example')}"

    assert _git("check-ignore", "-v", "--no-index", operator_file).strip(), (
        f"{operator_file} ships an example but has no ignore rule, so an operator's copy "
        "can be committed"
    )
    assert f"config/local/{example}" in set(_git("ls-files").splitlines()), (
        f"config/local/{example} is ignored or missing, so an operator has no template to copy"
    )


def test_project_compose_overlays_are_ignored():
    assert _git("check-ignore", "-v", "--no-index", "compose.pilot.yaml").strip()


def test_the_platform_compose_files_stay_tracked():
    tracked = set(_git("ls-files").splitlines())

    assert {"compose.yaml", "compose.override.yaml"} <= tracked


def _setup_python_steps() -> list[tuple[str, dict]]:
    import yaml

    steps = []
    for workflow in sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml")):
        jobs = yaml.safe_load(workflow.read_text())["jobs"]
        for job in jobs.values():
            for step in job.get("steps", []):
                steps.append((workflow.name, step))
    return steps


def test_every_workflow_takes_python_from_the_python_version_file():
    setup = [
        (name, step.get("with", {}))
        for name, step in _setup_python_steps()
        if str(step.get("uses", "")).startswith("actions/setup-python@")
    ]
    assert setup
    assert [(name, inputs) for name, inputs in setup if "python-version" in inputs] == []
    assert {inputs.get("python-version-file") for _, inputs in setup} == {".python-version"}


def test_no_workflow_overrides_the_python_version_of_uv():
    overrides = [
        (name, step["run"])
        for name, step in _setup_python_steps()
        if "uv " in str(step.get("run", "")) and "--python" in str(step.get("run", ""))
    ]
    assert overrides == []


# Each pattern captures the minor version of a Python 3 reference.
_PYTHON_VERSION_PATTERNS = (
    r"python3\.(\d+)",
    r"python:3\.(\d+)",
    r"pip3\.(\d+)",
    r"[Pp]ython 3\.(\d+)",
    r"\bpy3(\d\d)\b",
    r"--python-version 3\.(\d+)",
    r"requires-python = \">=3\.(\d+)\"",
    r"pythonVersion = \"3\.(\d+)\"",
)
# Records of past runs, and the lock file, whose wheel tags name every Python a package supports.
_PYTHON_VERSION_HISTORY = ("uv.lock", "docs/plans/", "services/repository_intelligence/eval/")


def test_every_python_reference_names_the_python_version_file():
    import re

    pinned = (REPO_ROOT / ".python-version").read_text().strip().split(".")[1]
    pattern = re.compile("|".join(_PYTHON_VERSION_PATTERNS))
    listed = subprocess.run(
        ["git", "grep", "-I", "-n", "-E", "python|pip3|py3[0-9]", "--", "."],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    stale = []
    for line in listed.splitlines():
        path, number, text = line.split(":", 2)
        if path.startswith(_PYTHON_VERSION_HISTORY):
            continue
        for match in pattern.finditer(text):
            minor = next(group for group in match.groups() if group is not None)
            if minor != pinned:
                stale.append(f"{path}:{number}: {match.group(0)}")
    assert stale == []
