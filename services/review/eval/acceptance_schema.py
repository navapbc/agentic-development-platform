"""Grounded acceptance-case schema and loader for the acceptance gate eval.

Each case is a committed fixture directory carrying the ticket's acceptance
criteria, the completion summary a run would report, a met or unmet ground-truth
outcome, and a ``base`` and ``head`` file tree. The harness materializes the two
trees into a temporary git repository so the acceptance gate reviews a real
base-to-change diff, the way production reviews an implemented change grounded at
its commit. The labels here define the ground truth the gate is measured against.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from services import process

OUTCOME_MET = "met"
OUTCOME_UNMET = "unmet"
OUTCOMES = (OUTCOME_MET, OUTCOME_UNMET)

_CASE_FILE = "case.json"
_BASE_DIR = "base"
_HEAD_DIR = "head"


class AcceptanceSetError(Exception):
    """The acceptance set is missing or malformed."""


@dataclass(frozen=True)
class AcceptanceCase:
    case_id: str
    acceptance_criteria: str
    change_summary: str
    outcome: str
    base_dir: Path
    head_dir: Path
    notes: str | None = None

    def changed_files(self) -> list[str]:
        base_files = _tree(self.base_dir)
        head_files = _tree(self.head_dir)
        names = sorted(set(base_files) | set(head_files))
        return [name for name in names if base_files.get(name) != head_files.get(name)]


def _tree(root: Path) -> dict[str, str]:
    if not root.is_dir():
        return {}
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[str(path.relative_to(root))] = path.read_text(encoding="utf-8")
    return out


def _parse_case(case_dir: Path) -> AcceptanceCase:
    manifest = case_dir / _CASE_FILE
    record = json.loads(manifest.read_text(encoding="utf-8"))
    for field_name in ("case_id", "acceptance_criteria", "change_summary", "outcome"):
        value = record.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise AcceptanceSetError(f"case {case_dir.name!r} requires a non-empty {field_name!r}")
    if record["outcome"] not in OUTCOMES:
        raise AcceptanceSetError(f"case {case_dir.name!r} outcome must be one of {OUTCOMES}")
    head_dir = case_dir / _HEAD_DIR
    if not head_dir.is_dir():
        raise AcceptanceSetError(f"case {case_dir.name!r} is missing a {_HEAD_DIR}/ tree")
    case = AcceptanceCase(
        case_id=record["case_id"],
        acceptance_criteria=record["acceptance_criteria"],
        change_summary=record["change_summary"],
        outcome=record["outcome"],
        base_dir=case_dir / _BASE_DIR,
        head_dir=head_dir,
        notes=record.get("notes") if isinstance(record.get("notes"), str) else None,
    )
    if not case.changed_files():
        raise AcceptanceSetError(f"case {case_dir.name!r} has no diff between base and head")
    return case


def read_acceptance_set(root: str | Path) -> list[AcceptanceCase]:
    """Load and validate every case under ``root``, requiring both outcomes present."""
    resolved = Path(root)
    if not resolved.is_dir():
        raise AcceptanceSetError(f"acceptance set directory not found: {resolved}")
    cases: list[AcceptanceCase] = []
    seen: set[str] = set()
    for case_dir in sorted(p for p in resolved.iterdir() if p.is_dir()):
        if not (case_dir / _CASE_FILE).is_file():
            continue
        case = _parse_case(case_dir)
        if case.case_id in seen:
            raise AcceptanceSetError(f"duplicate case id {case.case_id!r}")
        seen.add(case.case_id)
        cases.append(case)
    if not cases:
        raise AcceptanceSetError(f"acceptance set is empty: {resolved}")
    outcomes = {case.outcome for case in cases}
    if OUTCOME_MET not in outcomes or OUTCOME_UNMET not in outcomes:
        raise AcceptanceSetError("acceptance set must contain at least one met and one unmet case")
    return cases


def materialize_case(case: AcceptanceCase, workdir: Path) -> tuple[str, str]:
    """Build a git repo in ``workdir`` from the case's base and head trees.

    Commits the base tree, then the head tree, so the head commit carries the
    change as a real base-to-head diff. Returns ``(repo_root, head_sha)``.
    """

    def git(*args: str) -> str:
        result = process.run(
            ["git", "-C", str(workdir), *args],
            timeout=process.SHORT_COMMAND_TIMEOUT_SECONDS,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    workdir.mkdir(parents=True, exist_ok=True)
    git("init", "-q", "-b", "main")
    git("config", "user.email", "acceptance-eval@nava.invalid")
    git("config", "user.name", "acceptance-eval")
    _apply_tree(case.base_dir, workdir)
    git("add", "-A")
    git("commit", "-q", "--allow-empty", "-m", "base")
    _apply_tree(case.head_dir, workdir, clear_existing=True)
    git("add", "-A")
    git("commit", "-q", "--allow-empty", "-m", f"change for {case.case_id}")
    return str(workdir), git("rev-parse", "HEAD")


def _apply_tree(tree: Path, dest: Path, *, clear_existing: bool = False) -> None:
    if clear_existing:
        for path in dest.iterdir():
            if path.name == ".git":
                continue
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
    if not tree.is_dir():
        return
    for src in sorted(tree.rglob("*")):
        if src.is_file():
            target = dest / src.relative_to(tree)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")


def snapshot_case(
    *,
    repo_root: str | Path,
    base_ref: str,
    head_ref: str,
    case_id: str,
    acceptance_criteria: str,
    change_summary: str,
    outcome: str,
    dest_root: str | Path,
) -> Path:
    """Capture a repository's base-to-head diff into a labeled acceptance fixture.

    Reads every file that differs between ``base_ref`` and ``head_ref`` and writes
    its base and head content under ``dest_root/case_id`` alongside a ``case.json``,
    turning an implemented change into a case the harness can score offline. Returns
    the created case directory.
    """
    if outcome not in OUTCOMES:
        raise AcceptanceSetError(f"outcome must be one of {OUTCOMES}")
    repo = Path(repo_root)

    def git(*args: str) -> str:
        result = process.run(
            ["git", "-C", str(repo), *args],
            timeout=process.SHORT_COMMAND_TIMEOUT_SECONDS,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout

    changed = [line for line in git("diff", "--name-only", base_ref, head_ref).splitlines() if line]
    if not changed:
        raise AcceptanceSetError(f"no diff between {base_ref!r} and {head_ref!r}")

    case_dir = Path(dest_root) / case_id
    if case_dir.exists():
        raise AcceptanceSetError(f"case directory already exists: {case_dir}")
    base_dir = case_dir / _BASE_DIR
    head_dir = case_dir / _HEAD_DIR

    def _write(ref: str, name: str, dest: Path) -> None:
        result = process.run(
            ["git", "-C", str(repo), "show", f"{ref}:{name}"],
            timeout=process.SHORT_COMMAND_TIMEOUT_SECONDS,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return
        target = dest / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(result.stdout, encoding="utf-8")

    for name in changed:
        _write(base_ref, name, base_dir)
        _write(head_ref, name, head_dir)

    manifest = {
        "case_id": case_id,
        "acceptance_criteria": acceptance_criteria,
        "change_summary": change_summary,
        "outcome": outcome,
    }
    (case_dir / _CASE_FILE).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    _parse_case(case_dir)
    return case_dir
