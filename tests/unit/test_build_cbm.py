"""Unit tests for the CBM build/vendor script command construction and flow."""

from __future__ import annotations

from scripts.build_cbm import (
    DEFAULT_REF,
    DEFAULT_URL,
    build,
    build_command,
    clone_command,
)


def test_clone_command_pins_the_ref_and_depth():
    cmd = clone_command("https://example.com/cbm.git", "v0.9.0", "/tmp/cbm")
    assert cmd == [
        "git",
        "clone",
        "--depth",
        "1",
        "--branch",
        "v0.9.0",
        "--",
        "https://example.com/cbm.git",
        "/tmp/cbm",
    ]


def test_build_command_targets_the_export_stage_and_dest():
    cmd = build_command("/src/cbm", "vendor/bin")
    assert cmd == [
        "docker",
        "build",
        "-f",
        "docker/cbm-build.Dockerfile",
        "--target",
        "export",
        "--output",
        "type=local,dest=vendor/bin",
        "/src/cbm",
    ]


def test_default_source_is_the_upstream_repo_at_a_pinned_tag():
    assert DEFAULT_URL.endswith("codebase-memory-mcp.git")
    assert DEFAULT_REF == "v0.9.0"


def test_build_from_local_source_skips_the_clone():
    calls: list[list[str]] = []
    out = build(
        source="/src/cbm",
        url=DEFAULT_URL,
        ref=DEFAULT_REF,
        dest="vendor/bin",
        runner=calls.append,
    )
    assert calls == [build_command("/src/cbm", "vendor/bin")]
    assert out == "vendor/bin/codebase-memory-mcp"


def test_build_without_source_clones_then_builds():
    calls: list[list[str]] = []
    build(
        source=None,
        url="https://example.com/cbm.git",
        ref="v0.9.0",
        dest="vendor/bin",
        runner=calls.append,
    )
    assert len(calls) == 2
    assert calls[0][0] == "git"
    assert calls[0][:2] == ["git", "clone"]
    assert calls[1][0] == "docker"
    # the clone target and the build context are the same temporary directory
    clone_target = calls[0][-1]
    build_context = calls[1][-1]
    assert clone_target == build_context
