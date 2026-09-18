"""The code-review gate's deterministic scanners must read this repository's source, nothing else.

The gate points gitleaks and semgrep at the working checkout with their ignore logic disabled
(``--no-git`` and ``--no-git-ignore``), so without these ignore files they descend into the agent
workspace root, which holds full clones of the repositories the platform is working on. Each tool
then overruns the grounding harness's per-call budget and abstains, and an abstain on a fail-closed
criterion turns the verdict into a BLOCK carrying no findings.
"""

from __future__ import annotations

import json
import pathlib
import random
import shutil
import string
import subprocess

import pytest
from scripts.config import Settings

PLATFORM_ROOT = pathlib.Path(__file__).resolve().parents[2]
GITLEAKS_CONFIG = PLATFORM_ROOT / ".gitleaks.toml"
SEMGREP_IGNORE = PLATFORM_ROOT / ".semgrepignore"

WORKSPACE_ROOT = Settings.model_fields["execution_workspace_root"].default
NON_SOURCE_TREES = (WORKSPACE_ROOT, ".venv", "vendor")

_TOKEN_ALPHABET = string.ascii_letters + string.digits
_TOKEN_RNG = random.Random(7)
PLANTED_SECRET = "ghp_" + "".join(_TOKEN_RNG.choice(_TOKEN_ALPHABET) for _ in range(36))
"""A synthetic token. gitleaks scores entropy, so a patterned literal is not detected at all."""

SEMGREP_RULE = """rules:
  - id: planted-eval
    languages: [python]
    severity: WARNING
    message: eval call
    pattern: eval(...)
"""


def _leaky_module() -> str:
    return f'TOKEN = "{PLANTED_SECRET}"\neval("1")\n'


@pytest.fixture
def scan_root(tmp_path: pathlib.Path) -> pathlib.Path:
    """A checkout-shaped tree carrying the planted module in source and every non-source tree."""
    root = tmp_path / "checkout"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text(_leaky_module(), encoding="utf-8")
    for tree in NON_SOURCE_TREES:
        nested = root / tree / "clone" / "src"
        nested.mkdir(parents=True)
        (nested / "app.py").write_text(_leaky_module(), encoding="utf-8")
    for config in (GITLEAKS_CONFIG, SEMGREP_IGNORE):
        if config.exists():
            shutil.copy(config, root / config.name)
    return root


def _scanned_files(sarif: dict, root: pathlib.Path) -> set[str]:
    found: set[str] = set()
    for run in sarif.get("runs", []):
        for result in run.get("results", []):
            for location in result.get("locations", []):
                uri = location["physicalLocation"]["artifactLocation"]["uri"]
                path = pathlib.Path(uri)
                found.add(str(path.relative_to(root) if path.is_absolute() else path))
    return found


@pytest.mark.parametrize("tree", NON_SOURCE_TREES)
def test_both_scanners_are_told_to_skip_the_non_source_tree(tree: str) -> None:
    assert tree in GITLEAKS_CONFIG.read_text(encoding="utf-8")
    assert tree in SEMGREP_IGNORE.read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("gitleaks") is None, reason="gitleaks is not installed")
def test_gitleaks_reads_the_source_tree_and_no_other(scan_root: pathlib.Path) -> None:
    report = scan_root.parent / "gitleaks.sarif"
    subprocess.run(
        [
            "gitleaks",
            "detect",
            "--no-banner",
            "--no-git",
            "--source",
            str(scan_root),
            "--report-format",
            "sarif",
            "--report-path",
            str(report),
            "--exit-code",
            "0",
        ],
        check=True,
        capture_output=True,
        cwd=scan_root,
    )

    scanned = _scanned_files(json.loads(report.read_text(encoding="utf-8")), scan_root)

    assert "src/app.py" in scanned
    assert not [path for path in scanned if path.startswith(NON_SOURCE_TREES)]


@pytest.mark.skipif(
    shutil.which("opengrep") is None and shutil.which("semgrep") is None,
    reason="neither opengrep nor semgrep is installed",
)
def test_semgrep_reads_the_source_tree_and_no_other(scan_root: pathlib.Path) -> None:
    binary = shutil.which("opengrep") or shutil.which("semgrep")
    assert binary is not None
    rule = scan_root.parent / "rule.yaml"
    rule.write_text(SEMGREP_RULE, encoding="utf-8")

    proc = subprocess.run(
        [
            binary,
            "scan",
            "--sarif",
            "--metrics=off",
            "--no-git-ignore",
            "--config",
            str(rule),
            str(scan_root),
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=scan_root,
    )

    scanned = _scanned_files(json.loads(proc.stdout), scan_root)

    assert "src/app.py" in scanned
    assert not [path for path in scanned if path.startswith(NON_SOURCE_TREES)]
