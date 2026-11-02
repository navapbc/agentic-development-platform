"""Create and audit git worktrees so each one owns an independent virtualenv.

The project installs itself as an editable package, and an editable install records one
absolute source path inside site-packages. A virtualenv shared between worktrees therefore
resolves imports to whichever worktree ran `uv sync` last, so the other worktrees silently
import a source tree they are not editing and their test results describe the wrong branch.
Sharing a virtualenv by symlinking `.venv` saves disk at the cost of that silent mix-up, so
`new` provisions a separate virtualenv per worktree and `check` reports any that regressed.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from services import process

PYTHON_VERSION = f"{sys.version_info.major}.{sys.version_info.minor}"
SYNC_TIMEOUT_SECONDS = process.LONG_COMMAND_TIMEOUT_SECONDS


class VenvState(StrEnum):
    OK = "ok"
    MISSING = "missing"
    SHARED = "shared"
    MISTARGETED = "mistargeted"
    SKIPPED = "skipped"


_REPAIRABLE = frozenset({VenvState.MISSING, VenvState.SHARED, VenvState.MISTARGETED})
_HEALTHY = frozenset({VenvState.OK, VenvState.SKIPPED})
_SITE_PACKAGES_PATTERNS = ("lib/python*/site-packages", "Lib/site-packages")


@dataclass(frozen=True)
class VenvStatus:
    worktree: Path
    state: VenvState
    detail: str

    @property
    def healthy(self) -> bool:
        return self.state in _HEALTHY


def _git(args: list[str], *, cwd: Path) -> str:
    result = process.run(
        ["git", *args],
        cwd=str(cwd),
        timeout=process.SHORT_COMMAND_TIMEOUT_SECONDS,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout


def repo_root(start: Path) -> Path:
    return Path(_git(["rev-parse", "--show-toplevel"], cwd=start).strip())


def worktree_paths(root: Path) -> list[Path]:
    listing = _git(["worktree", "list", "--porcelain"], cwd=root)
    return [
        Path(line.removeprefix("worktree ").strip())
        for line in listing.splitlines()
        if line.startswith("worktree ")
    ]


def _editable_targets(venv: Path) -> list[str]:
    """Return the source directories the virtualenv's editable installs resolve imports to.

    A line is only a candidate when it names a directory holding a `pyproject.toml`, so an
    unrelated editable install elsewhere on the machine is not mistaken for this project.
    """
    targets: list[str] = []
    for pattern in _SITE_PACKAGES_PATTERNS:
        for site_packages in venv.glob(pattern):
            for pth in site_packages.glob("*.pth"):
                for line in pth.read_text(encoding="utf-8", errors="replace").splitlines():
                    candidate = line.strip()
                    if (
                        candidate
                        and Path(candidate).is_absolute()
                        and (Path(candidate) / "pyproject.toml").is_file()
                    ):
                        targets.append(candidate)
    return targets


def inspect(worktree: Path) -> VenvStatus:
    if not (worktree / "pyproject.toml").is_file():
        return VenvStatus(worktree, VenvState.SKIPPED, "not a checkout of this project")
    venv = worktree / ".venv"
    if venv.is_symlink():
        return VenvStatus(worktree, VenvState.SHARED, f".venv is a symlink to {os.readlink(venv)}")
    if not venv.is_dir():
        return VenvStatus(worktree, VenvState.MISSING, "no .venv directory")
    targets = _editable_targets(venv)
    if not targets:
        return VenvStatus(
            worktree, VenvState.MISSING, "no editable install recorded in site-packages"
        )
    owner = worktree.resolve()
    foreign = sorted({target for target in targets if Path(target).resolve() != owner})
    if foreign:
        return VenvStatus(
            worktree, VenvState.MISTARGETED, f"editable install points at {foreign[0]}"
        )
    return VenvStatus(worktree, VenvState.OK, str(venv))


def provision(worktree: Path, *, python_version: str = PYTHON_VERSION) -> None:
    """Give the worktree its own virtualenv, replacing a symlink that shares another one."""
    venv = worktree / ".venv"
    if venv.is_symlink():
        venv.unlink()
    process.run(
        ["uv", "sync", "--locked", "--all-extras", "--python", python_version],
        cwd=str(worktree),
        timeout=SYNC_TIMEOUT_SECONDS,
        check=True,
    )
    status = inspect(worktree)
    if status.state is not VenvState.OK:
        raise RuntimeError(f"{worktree}: virtualenv still unhealthy after sync: {status.detail}")


def _add(root: Path, path: Path, branch: str, *, create: bool) -> None:
    spec = ["-b", branch] if create else [branch]
    _git(["worktree", "add", *spec, str(path)], cwd=root)


def command_new(args: argparse.Namespace) -> int:
    root = repo_root(Path.cwd())
    path = Path(args.path).expanduser().resolve()
    if path.exists():
        print(f"[worktree] {path} already exists", file=sys.stderr)
        return 1
    _add(root, path, args.branch, create=args.create)
    print(f"[worktree] added {path} on {args.branch}")
    provision(path, python_version=args.python)
    print(f"[worktree] provisioned an independent virtualenv at {path}/.venv")
    return 0


def command_check(args: argparse.Namespace) -> int:
    root = repo_root(Path.cwd())
    statuses = [inspect(worktree) for worktree in worktree_paths(root)]
    failures: list[str] = []
    if args.repair:
        for status in [s for s in statuses if s.state in _REPAIRABLE]:
            print(f"[worktree] repairing {status.worktree}: {status.detail}")
            try:
                provision(status.worktree, python_version=args.python)
            except (
                RuntimeError,
                OSError,
                process.CalledProcessError,
                process.TimeoutExpired,
            ) as error:
                failures.append(f"{status.worktree}: {error}")
        statuses = [inspect(worktree) for worktree in worktree_paths(root)]
    width = max((len(str(s.worktree)) for s in statuses), default=0)
    for status in statuses:
        print(f"{str(status.worktree):<{width}}  {status.state:<12} {status.detail}")
    for failure in failures:
        print(f"[worktree] repair failed for {failure}")
    unhealthy = [s for s in statuses if not s.healthy]
    if unhealthy:
        print(
            f"[worktree] {len(unhealthy)} worktree(s) do not own a correct virtualenv. "
            "Rerun with --repair."
        )
    return 1 if unhealthy or failures else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="worktree", description=__doc__)
    parser.add_argument("--python", default=PYTHON_VERSION, help="Python version for the venv.")
    sub = parser.add_subparsers(dest="command", required=True)

    new = sub.add_parser("new", help="Add a worktree and provision its own virtualenv.")
    new.add_argument("path", help="Directory to create the worktree in.")
    new.add_argument("branch", help="Branch to check out in the worktree.")
    new.add_argument("--create", action="store_true", help="Create the branch.")
    new.set_defaults(handler=command_new)

    check = sub.add_parser("check", help="Report worktrees that share or lack a virtualenv.")
    check.add_argument("--repair", action="store_true", help="Provision the ones that are wrong.")
    check.set_defaults(handler=command_check)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except FileNotFoundError as error:
        print(f"[worktree] {error.strerror or error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
