"""Tests for attributing security findings to the files a change touched."""

from __future__ import annotations

import logging

import pytest
from adapters.interfaces.security_scanner import CRITICAL, STATIC, ScanFinding
from services.orchestration.gates.scan_scope import (
    blocking_findings,
    changed_paths,
    is_attributable,
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


async def test_the_changed_set_comes_from_a_merge_base_diff_of_the_two_refs():
    seen: list[tuple[list[str], str]] = []

    async def run(args, cwd):
        seen.append((list(args), cwd))
        return 0, "portal/src/a.tsx\0portal/src/b.tsx\0", ""

    changed = await changed_paths("/ws", "base1", "head1", runner=run)

    assert changed == frozenset({"portal/src/a.tsx", "portal/src/b.tsx"})
    assert seen == [(["git", "diff", "--name-only", "-z", "base1...head1"], "/ws")]


async def test_a_path_outside_ascii_is_not_c_quoted_so_it_matches_its_finding(tmp_path):
    name = "portal/src/caf\u00e9.tsx"

    async def run(args, cwd):
        assert "-z" in args
        return 0, f"{name}\0", ""

    changed = await changed_paths(str(tmp_path), "base1", "head1", runner=run)

    assert changed == frozenset({name})
    assert is_attributable(_finding(f"{name}:4"), changed, str(tmp_path)) is True


async def test_an_empty_ref_falls_back_to_the_repository_and_blocks(caplog):
    with caplog.at_level(logging.WARNING):
        changed = await changed_paths("/ws", "base1", "")

    assert changed is None
    assert "does not describe a range" in caplog.text


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
        changed = await changed_paths("/ws", "", "head1")

    assert changed is None
    assert "falling back to the repository" in caplog.text
    finding = _finding("anywhere.py:1")
    assert blocking_findings([finding], changed, "/ws") == [finding]


async def test_a_failing_git_call_falls_back_to_the_repository_and_blocks(caplog):
    with caplog.at_level(logging.WARNING):
        changed = await changed_paths(
            "/ws", "base1", "head1", runner=_runner("", code=128, stderr="bad revision")
        )

    assert changed is None
    assert "exited 128" in caplog.text
    assert "bad revision" in caplog.text


async def test_git_failing_to_start_falls_back_to_the_repository_and_blocks(caplog):
    async def run(args, cwd):
        raise OSError("no git on this worker")

    with caplog.at_level(logging.WARNING):
        changed = await changed_paths("/ws", "base1", "head1", runner=run)

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
