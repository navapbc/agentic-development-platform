"""Tests for attributing security findings to the files a change touched."""

from __future__ import annotations

import json
import logging
import subprocess

import pytest
from adapters.interfaces.security_scanner import CRITICAL, DEPENDENCY, STATIC, ScanFinding
from services.orchestration.gates.scan_scope import (
    blocking_findings,
    changed_paths,
    dependency_baseline,
    is_attributable,
    packages_in,
    relative_location_path,
)


def _finding(location: str, finding_id: str = "f1") -> ScanFinding:
    return ScanFinding(
        finding_id=finding_id,
        scanner="semgrep",
        kind=STATIC,
        severity=CRITICAL,
        title="t",
        location=location,
    )


def _runner(stdout: str, code: int = 0, stderr: str = ""):
    async def run(args, cwd):
        return code, stdout, stderr

    return run


class _GitStub:
    """A runner that answers each of the three git calls changed_paths makes."""

    def __init__(self, merge_base: str, tracked: str, untracked: str) -> None:
        self._merge_base = merge_base
        self._tracked = tracked
        self._untracked = untracked
        self.seen: list[list[str]] = []

    async def __call__(self, args, cwd):
        self.seen.append(list(args))
        if args[1] == "merge-base":
            return 0, self._merge_base, ""
        if args[1] == "diff":
            return 0, self._tracked, ""
        return 0, self._untracked, ""


def _git_stub(*, merge_base: str = "mb1\n", tracked: str = "", untracked: str = "") -> _GitStub:
    return _GitStub(merge_base, tracked, untracked)


async def test_the_changed_set_is_the_working_tree_against_the_merge_base():
    run = _git_stub(merge_base="mb1\n", tracked="portal/src/a.tsx\0portal/src/b.tsx\0")

    changed = await changed_paths("/ws", "base1", runner=run)

    assert changed == frozenset({"portal/src/a.tsx", "portal/src/b.tsx"})
    assert run.seen == [
        ["git", "merge-base", "base1", "HEAD"],
        ["git", "diff", "--name-only", "-z", "mb1"],
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
    ]


async def test_the_change_set_is_not_read_from_the_pushed_commit():
    """The push builds its commit in a throwaway clone, so it resolves no object here."""
    run = _git_stub(tracked="portal/src/a.tsx\0")

    await changed_paths("/ws", "base1", runner=run)

    assert not any("..." in arg for args in run.seen for arg in args)


async def test_a_file_the_change_added_is_attributed_to_it(tmp_path):
    (tmp_path / "added.py").write_text("x = 1\n")
    run = _git_stub(tracked="", untracked="added.py\0")

    changed = await changed_paths(str(tmp_path), "base1", runner=run)

    assert changed == frozenset({"added.py"})
    assert changed is not None
    assert is_attributable(_finding("added.py:1"), changed, str(tmp_path)) is True


async def test_an_unresolvable_merge_base_falls_back_to_the_repository_and_blocks(caplog):
    async def run(args, cwd):
        if args[1] == "merge-base":
            return 128, "", "Not a valid object name"
        return 0, "", ""

    with caplog.at_level(logging.WARNING):
        changed = await changed_paths("/ws", "base1", runner=run)

    assert changed is None
    assert "Not a valid object name" in caplog.text


async def test_an_untracked_listing_that_fails_falls_back_to_the_repository_and_blocks(caplog):
    async def run(args, cwd):
        if args[1] == "ls-files":
            return 128, "", "index is locked"
        if args[1] == "merge-base":
            return 0, "mb1\n", ""
        return 0, "portal/src/a.tsx\0", ""

    with caplog.at_level(logging.WARNING):
        changed = await changed_paths("/ws", "base1", runner=run)

    assert changed is None
    assert "index is locked" in caplog.text


async def test_a_path_outside_ascii_is_not_c_quoted_so_it_matches_its_finding(tmp_path):
    name = "portal/src/caf\u00e9.tsx"
    run = _git_stub(tracked=f"{name}\0")

    changed = await changed_paths(str(tmp_path), "base1", runner=run)

    assert changed == frozenset({name})
    assert changed is not None
    assert is_attributable(_finding(f"{name}:4"), changed, str(tmp_path)) is True
    assert all("-z" in args for args in run.seen if args[1] != "merge-base")


async def test_a_finding_in_an_unchanged_file_cannot_set_the_verdict(tmp_path):
    (tmp_path / "untouched.py").write_text("x = 1\n")
    changed = frozenset({"changed.py"})

    assert blocking_findings([_finding("untouched.py:12")], changed, str(tmp_path)) == []


async def test_a_finding_in_a_changed_file_still_sets_the_verdict(tmp_path):
    (tmp_path / "changed.py").write_text("x = 1\n")
    finding = _finding("changed.py:12")

    assert blocking_findings([finding], frozenset({"changed.py"}), str(tmp_path)) == [finding]


async def test_a_pod_mode_location_is_matched_against_the_checkout_relative_path(tmp_path):
    (tmp_path / "changed.py").write_text("x = 1\n")
    finding = _finding(f"{tmp_path}/changed.py:12")

    assert blocking_findings([finding], frozenset({"changed.py"}), str(tmp_path)) == [finding]


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("app/run.py:12", "app/run.py"),
        ("app/run.py", "app/run.py"),
        ("requirements.txt:requests@2.0.0", "requirements.txt"),
        ("", ""),
    ],
)
def test_the_path_is_the_segment_before_the_first_colon(location, expected):
    assert relative_location_path(location, "/ws") == expected


async def test_an_empty_base_ref_falls_back_to_the_repository_and_blocks(caplog):
    with caplog.at_level(logging.WARNING):
        changed = await changed_paths("/ws", "")

    assert changed is None
    assert "falling back to the repository" in caplog.text
    finding = _finding("anywhere.py:1")
    assert blocking_findings([finding], changed, "/ws") == [finding]


async def test_a_failing_git_call_falls_back_to_the_repository_and_blocks(caplog):
    with caplog.at_level(logging.WARNING):
        changed = await changed_paths(
            "/ws", "base1", runner=_runner("", code=128, stderr="bad revision")
        )

    assert changed is None
    assert "exited 128" in caplog.text
    assert "bad revision" in caplog.text


async def test_git_failing_to_start_falls_back_to_the_repository_and_blocks(caplog):
    async def run(args, cwd):
        raise OSError("no git on this worker")

    with caplog.at_level(logging.WARNING):
        changed = await changed_paths("/ws", "base1", runner=run)

    assert changed is None
    assert "no git on this worker" in caplog.text


def test_a_finding_with_no_location_keeps_its_power_to_block(tmp_path, caplog):
    finding = _finding("")

    with caplog.at_level(logging.WARNING):
        result = blocking_findings([finding], frozenset({"changed.py"}), str(tmp_path))

    assert result == [finding]
    assert "carries no location" in caplog.text


def test_a_location_naming_no_file_in_the_checkout_keeps_its_power_to_block(tmp_path, caplog):
    finding = _finding("Node.js:lodash@4.17.0")

    with caplog.at_level(logging.WARNING):
        result = blocking_findings([finding], frozenset({"changed.py"}), str(tmp_path))

    assert result == [finding]
    assert "not a file in the checkout" in caplog.text


def test_a_path_sharing_a_prefix_with_a_changed_path_is_not_attributed_to_it(tmp_path):
    (tmp_path / "save-profile.tsx.bak").write_text("x\n")
    changed = frozenset({"save-profile.tsx"})

    assert is_attributable(_finding("save-profile.tsx.bak:3"), changed, str(tmp_path)) is False


def test_attribution_keeps_every_finding_available_as_evidence(tmp_path):
    (tmp_path / "untouched.py").write_text("x = 1\n")
    (tmp_path / "changed.py").write_text("x = 1\n")
    findings = [_finding("untouched.py:1", "a"), _finding("changed.py:1", "b")]

    scoped = blocking_findings(findings, frozenset({"changed.py"}), str(tmp_path))

    assert [f.finding_id for f in scoped] == ["b"]
    assert [f.finding_id for f in findings] == ["a", "b"]


async def test_a_diff_reporting_no_paths_falls_back_to_the_repository_and_blocks(caplog):
    with caplog.at_level(logging.WARNING):
        changed = await changed_paths("/ws", "base1", runner=_runner(""))

    assert changed is None
    assert "reported no paths" in caplog.text
    finding = _finding("anywhere.py:1")
    assert blocking_findings([finding], changed, "/ws") == [finding]


def _lock(**packages: str) -> str:
    return json.dumps(
        {
            "lockfileVersion": 3,
            "packages": {
                "": {"name": "app"},
                **{f"node_modules/{name}": {"version": v} for name, v in packages.items()},
            },
        }
    )


def _git(root, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _dependency(location: str) -> ScanFinding:
    return ScanFinding(location, "trivy", DEPENDENCY, CRITICAL, "t", location=location)


async def test_only_a_dependency_the_change_added_or_upgraded_sets_the_verdict(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.invalid")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "package-lock.json").write_text(_lock(lodash="4.17.15", minimist="1.2.0"))
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    (tmp_path / "package-lock.json").write_text(
        _lock(lodash="4.17.15", minimist="1.2.5", **{"@babel/core": "7.0.0"})
    )
    findings = [
        _dependency("package-lock.json:lodash@4.17.15"),
        _dependency("package-lock.json:minimist@1.2.5"),
        _dependency("package-lock.json:@babel/core@7.0.0"),
    ]
    changed = await changed_paths(str(tmp_path), "main")

    baseline = await dependency_baseline(str(tmp_path), "main", {"package-lock.json"})
    blocking = blocking_findings(findings, changed, str(tmp_path), baseline=baseline)

    assert [f.location for f in blocking] == [
        "package-lock.json:minimist@1.2.5",
        "package-lock.json:@babel/core@7.0.0",
    ]


def test_a_go_module_the_merge_base_held_matches_with_or_without_its_v_prefix():
    baseline = {"go.mod": packages_in("go.mod", "require github.com/a/b v1.2.3\n")}
    findings = [
        _dependency("go.mod:github.com/a/b@1.2.3"),
        _dependency("go.mod:github.com/a/b@v1.2.3"),
        _dependency("go.mod:github.com/a/b@1.3.0"),
    ]

    blocking = blocking_findings(findings, frozenset({"go.mod"}), "/ws", baseline=baseline)

    assert [f.location for f in blocking] == ["go.mod:github.com/a/b@1.3.0"]


@pytest.mark.parametrize(
    ("path", "text", "expected"),
    [
        ("web/package-lock.json", _lock(**{"@babel/core": "7.0.0"}), {("@babel/core", "7.0.0")}),
        (
            "go.sum",
            "github.com/google/uuid v1.6.0 h1:abc=\ngithub.com/google/uuid v1.6.0/go.mod h1:d=\n",
            {("github.com/google/uuid", "1.6.0")},
        ),
        ("go.mod", "module x\n\nrequire github.com/a/b v1.2.3\n", {("github.com/a/b", "1.2.3")}),
        (
            "requirements-dev.txt",
            "Flask_Login==0.6.3  # pinned\n-r base.txt\n",
            {("flask-login", "0.6.3")},
        ),
        ("pom.xml", "<project/>", None),
    ],
)
def test_a_dependency_file_lists_the_packages_its_format_names(path, text, expected):
    assert packages_in(path, text) == (None if expected is None else frozenset(expected))
