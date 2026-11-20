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
        except (UnicodeDecodeError, OSError):
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
