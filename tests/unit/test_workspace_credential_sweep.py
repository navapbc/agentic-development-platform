"""The sweep clears the credential earlier releases persisted into workspace checkouts.

A workspace is keyed by execution id and nothing deletes it, so the scrub that runs when a
workspace is materialized again never reaches a checkout that is never reused.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from services.orchestration.workspace_scrub import (
    scrub_workspace_credentials,
    strip_userinfo,
)

TOKEN = "ghs-persisted-secret"
CLEAN = "https://acme.ghe/acme-org/agentic-demo-service.git"
EMBEDDED = f"https://x-access-token:{TOKEN}@acme.ghe/acme-org/agentic-demo-service.git"


def _checkout(path: Path, origin: str, *, pushurl: str = "") -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "remote", "add", "origin", origin],
        check=True,
        capture_output=True,
    )
    if pushurl:
        subprocess.run(
            ["git", "-C", str(path), "config", "remote.origin.pushurl", pushurl],
            check=True,
            capture_output=True,
        )
    return path


def _origin(path: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(path), "remote", "get-url", "origin"],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def test_the_sweep_rewrites_every_checkout_that_carries_an_embedded_credential(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspaces"
    first = _checkout(root / "repo" / "ADSER-1" / "exec-1", EMBEDDED)
    second = _checkout(root / "repo" / "ADSER-1" / "exec-2", EMBEDDED, pushurl=EMBEDDED)
    clean = _checkout(root / "repo" / "ADSER-2" / "exec-3", CLEAN)

    receipt = scrub_workspace_credentials(root)

    assert receipt.scanned == 3
    assert set(receipt.rewritten) == {str(first), str(second)}
    assert receipt.failed == ()
    assert _origin(first) == CLEAN
    assert _origin(second) == CLEAN
    assert _origin(clean) == CLEAN
    assert TOKEN not in (second / ".git" / "config").read_text(encoding="utf-8")


def test_a_second_sweep_finds_nothing_left_to_rewrite(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    _checkout(root / "repo" / "ADSER-1" / "exec-1", EMBEDDED)

    scrub_workspace_credentials(root)
    receipt = scrub_workspace_credentials(root)

    assert receipt.scanned == 1
    assert receipt.rewritten == ()


def test_a_missing_workspace_root_scans_nothing(tmp_path: Path) -> None:
    receipt = scrub_workspace_credentials(tmp_path / "absent")

    assert receipt == type(receipt)(scanned=0, rewritten=(), failed=())


def test_a_broken_checkout_is_reported_rather_than_stopping_the_sweep(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    broken = root / "repo" / "ADSER-1" / "exec-1"
    (broken / ".git").mkdir(parents=True)
    healthy = _checkout(root / "repo" / "ADSER-1" / "exec-2", EMBEDDED)

    receipt = scrub_workspace_credentials(root)

    assert receipt.scanned == 2
    assert receipt.failed == (str(broken),)
    assert receipt.rewritten == (str(healthy),)


def test_only_an_http_remote_loses_its_userinfo() -> None:
    assert strip_userinfo(EMBEDDED) == CLEAN
    assert strip_userinfo(CLEAN) == CLEAN
    assert strip_userinfo("git@acme.ghe:acme-org/repo.git") == "git@acme.ghe:acme-org/repo.git"
    assert strip_userinfo("/srv/git/repo.git") == "/srv/git/repo.git"
