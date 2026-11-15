"""Unit tests for the worktree virtualenv inspector."""

from __future__ import annotations

from pathlib import Path

import pytest
from scripts import worktree as worktree_module
from scripts.worktree import VenvState, inspect, provision


def _make_worktree(parent: Path, name: str) -> Path:
    worktree = parent / name
    worktree.mkdir(parents=True)
    (worktree / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    return worktree


def _make_venv(worktree: Path, editable_target: Path | None) -> Path:
    site_packages = worktree / ".venv" / "lib" / "python3.12" / "site-packages"
    site_packages.mkdir(parents=True)
    if editable_target is not None:
        editable_target.mkdir(parents=True, exist_ok=True)
        (editable_target / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        pth = site_packages / "_editable_impl_agentic_dev_platform.pth"
        pth.write_text(f"{editable_target}\n", encoding="utf-8")
    return worktree / ".venv"


def test_a_worktree_owning_its_virtualenv_is_healthy(tmp_path):
    worktree = _make_worktree(tmp_path, "adp-200")
    _make_venv(worktree, worktree)

    status = inspect(worktree)

    assert status.state == VenvState.OK
    assert status.healthy


def test_a_symlinked_virtualenv_is_reported_as_shared(tmp_path):
    owner = _make_worktree(tmp_path, "main")
    _make_venv(owner, owner)
    borrower = _make_worktree(tmp_path, "adp-201")
    (borrower / ".venv").symlink_to(owner / ".venv")

    status = inspect(borrower)

    assert status.state == VenvState.SHARED
    assert not status.healthy
    assert str(owner) in status.detail


def test_an_editable_install_pointing_at_another_worktree_is_mistargeted(tmp_path):
    worktree = _make_worktree(tmp_path, "adp-202")
    _make_venv(worktree, tmp_path / "adp-203")

    status = inspect(worktree)

    assert status.state == VenvState.MISTARGETED
    assert "adp-203" in status.detail


def test_a_worktree_without_a_virtualenv_is_missing(tmp_path):
    worktree = _make_worktree(tmp_path, "adp-204")

    assert inspect(worktree).state == VenvState.MISSING


def test_a_virtualenv_without_an_editable_install_cannot_be_verified(tmp_path):
    worktree = _make_worktree(tmp_path, "adp-205")
    _make_venv(worktree, None)

    status = inspect(worktree)

    assert status.state == VenvState.UNVERIFIED
    assert not status.healthy


def test_an_unverifiable_virtualenv_is_not_repaired(tmp_path):
    worktree = _make_worktree(tmp_path, "adp-209")
    _make_venv(worktree, None)

    assert inspect(worktree).state not in worktree_module._REPAIRABLE


def test_a_mistargeted_virtualenv_reports_every_foreign_target(tmp_path):
    worktree = _make_worktree(tmp_path, "adp-210")
    venv = _make_venv(worktree, tmp_path / "adp-211")
    other = tmp_path / "adp-212"
    other.mkdir()
    (other / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    site_packages = next(venv.glob("lib/python*/site-packages"))
    (site_packages / "_editable_impl_second.pth").write_text(f"{other}\n", encoding="utf-8")

    detail = inspect(worktree).detail

    assert "adp-211" in detail
    assert "adp-212" in detail


def test_an_unrelated_editable_install_does_not_make_a_worktree_mistargeted(tmp_path):
    worktree = _make_worktree(tmp_path, "adp-206")
    venv = _make_venv(worktree, worktree)
    other = tmp_path / "some-other-library"
    other.mkdir()
    site_packages = next(venv.glob("lib/python*/site-packages"))
    (site_packages / "__editable__.other.pth").write_text(f"{other}\n", encoding="utf-8")

    assert inspect(worktree).state == VenvState.OK


def test_a_worktree_without_a_project_file_is_skipped(tmp_path):
    worktree = tmp_path / "tickets-tracker"
    worktree.mkdir()

    status = inspect(worktree)

    assert status.state == VenvState.SKIPPED
    assert status.healthy


def test_a_windows_layout_virtualenv_is_recognised(tmp_path):
    worktree = _make_worktree(tmp_path, "adp-207")
    site_packages = worktree / ".venv" / "Lib" / "site-packages"
    site_packages.mkdir(parents=True)
    (site_packages / "_editable_impl_agentic_dev_platform.pth").write_text(
        f"{worktree}\n", encoding="utf-8"
    )

    assert inspect(worktree).state == VenvState.OK


def test_a_checkout_reached_through_a_symlinked_parent_is_not_mistargeted(tmp_path):
    real_parent = tmp_path / "real"
    worktree = _make_worktree(real_parent, "adp-208")
    _make_venv(worktree, worktree)
    (tmp_path / "linked").symlink_to(real_parent)

    assert inspect(tmp_path / "linked" / "adp-208").state == VenvState.OK


def test_provisioning_a_directory_that_is_not_a_checkout_raises(tmp_path):
    worktree = tmp_path / "not-a-checkout"
    worktree.mkdir()

    with pytest.raises(RuntimeError, match="not a checkout of this project"):
        provision(worktree)


def test_provisioning_accepts_a_virtualenv_whose_owner_cannot_be_verified(tmp_path, monkeypatch):
    worktree = _make_worktree(tmp_path, "adp-213")
    _make_venv(worktree, None)
    monkeypatch.setattr("scripts.worktree.process.run", lambda *args, **kwargs: None)

    provision(worktree)


def test_a_failed_provision_removes_the_worktree_it_just_added(tmp_path, monkeypatch, capsys):
    added = tmp_path / "adp-214"
    removed: list[list[str]] = []

    def fake_git(args, *, cwd):
        if args[:2] == ["worktree", "add"]:
            added.mkdir()
            return ""
        if args[:2] == ["worktree", "remove"]:
            removed.append(args)
            return ""
        return str(tmp_path)

    monkeypatch.setattr("scripts.worktree._git", fake_git)
    monkeypatch.setattr(
        "scripts.worktree.provision",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("uv sync exploded")),
    )

    args = worktree_module.build_parser().parse_args(["new", str(added), "some-branch"])

    assert args.handler(args) == 1
    assert removed and str(added) in removed[0]
    assert "uv sync exploded" in capsys.readouterr().err
