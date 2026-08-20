"""Unit tests for the engine source resolver."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from services.orchestration.gates.engine_source import (
    CommandResult,
    EngineSourceError,
    GateEngineConfig,
    resolve_engine_source,
)

pytestmark = pytest.mark.asyncio


class _FakeGit:
    def __init__(self, responses: dict[tuple[str, ...], CommandResult]) -> None:
        self._responses = responses
        self.calls: list[tuple[tuple[str, ...], str]] = []

    async def __call__(self, args, cwd):
        key = tuple(args)
        self.calls.append((key, cwd))
        for pattern, result in self._responses.items():
            if key[: len(pattern)] == pattern:
                return result
        return CommandResult(returncode=0, stdout="", stderr="")

    def ran(self, *prefix: str) -> bool:
        return any(call[0][: len(prefix)] == prefix for call in self.calls)


def _ok(stdout: str = "") -> CommandResult:
    return CommandResult(returncode=0, stdout=stdout, stderr="")


async def test_disabled_config_resolves_to_none():
    resolved = await resolve_engine_source(GateEngineConfig(enabled=False))
    assert resolved is None


async def test_local_mode_reports_the_checked_out_commit(tmp_path):
    (tmp_path / ".git").mkdir()
    runner = _FakeGit({("rev-parse", "HEAD"): _ok("abc123\n")})

    resolved = await resolve_engine_source(
        GateEngineConfig(enabled=True, mode="local", local_path=str(tmp_path)),
        runner=runner,
    )

    assert resolved is not None
    assert resolved.mode == "local"
    assert resolved.version == "abc123"
    assert resolved.path == str(tmp_path)


async def test_local_mode_fails_fast_when_path_missing():
    with pytest.raises(EngineSourceError):
        await resolve_engine_source(
            GateEngineConfig(enabled=True, mode="local", local_path="/no/such/dir"),
            runner=_FakeGit({}),
        )


async def test_tag_mode_clones_on_a_cache_miss(tmp_path):
    runner = _FakeGit(
        {
            ("ls-remote",): _ok("deadbeef\trefs/tags/v1.0.0^{}\n"),
            ("rev-parse", "HEAD"): _ok("deadbeef\n"),
        }
    )

    resolved = await resolve_engine_source(
        GateEngineConfig(
            enabled=True,
            mode="tag",
            repo_url="https://example.com/navapbc/ai-common-workflows.git",
            tag="v1.0.0",
            cache_root=str(tmp_path),
        ),
        runner=runner,
    )

    assert resolved is not None
    assert resolved.version == "deadbeef"
    assert resolved.tag == "v1.0.0"
    assert resolved.path.endswith("ai-common-workflows/v1.0.0")
    assert runner.ran("clone")
    assert not runner.ran("fetch")


async def test_tag_mode_uses_the_cache_without_pulling_when_unchanged(tmp_path):
    cache_path = tmp_path / "ai-common-workflows" / "v1.0.0"
    cache_path.mkdir(parents=True)
    runner = _FakeGit(
        {
            ("ls-remote",): _ok("deadbeef\trefs/tags/v1.0.0^{}\n"),
            ("rev-parse", "HEAD"): _ok("deadbeef\n"),
        }
    )

    resolved = await resolve_engine_source(
        GateEngineConfig(
            enabled=True,
            mode="tag",
            repo_url="https://example.com/navapbc/ai-common-workflows.git",
            tag="v1.0.0",
            cache_root=str(tmp_path),
        ),
        runner=runner,
    )

    assert resolved is not None
    assert resolved.version == "deadbeef"
    assert not runner.ran("clone")
    assert not runner.ran("fetch")


async def test_tag_mode_pulls_when_the_tag_moved(tmp_path):
    cache_path = tmp_path / "ai-common-workflows" / "v1.0.0"
    cache_path.mkdir(parents=True)
    head = {"value": "oldcommit"}

    class _MovingGit:
        def __init__(self) -> None:
            self.calls: list[tuple[str, ...]] = []

        async def __call__(self, args, cwd):
            key = tuple(args)
            self.calls.append(key)
            if key[0] == "ls-remote":
                return _ok("newcommit\trefs/tags/v1.0.0^{}\n")
            if key[0] == "rev-parse":
                return _ok(f"{head['value']}\n")
            if key[0] == "checkout":
                head["value"] = "newcommit"
                return _ok()
            return _ok()

        def ran(self, *prefix: str) -> bool:
            return any(call[: len(prefix)] == prefix for call in self.calls)

    runner = _MovingGit()

    resolved = await resolve_engine_source(
        GateEngineConfig(
            enabled=True,
            mode="tag",
            repo_url="https://example.com/navapbc/ai-common-workflows.git",
            tag="v1.0.0",
            cache_root=str(tmp_path),
        ),
        runner=runner,
    )

    assert resolved is not None
    assert resolved.version == "newcommit"
    assert runner.ran("fetch")
    assert runner.ran("checkout")


async def test_tag_mode_fails_fast_on_a_missing_tag(tmp_path):
    runner = _FakeGit({("ls-remote",): _ok("")})

    with pytest.raises(EngineSourceError):
        await resolve_engine_source(
            GateEngineConfig(
                enabled=True,
                mode="tag",
                repo_url="https://example.com/navapbc/ai-common-workflows.git",
                tag="missing",
                cache_root=str(tmp_path),
            ),
            runner=runner,
        )


async def test_tag_mode_fails_fast_when_git_errors(tmp_path):
    runner = _FakeGit(
        {("ls-remote",): CommandResult(returncode=128, stdout="", stderr="network down")}
    )

    with pytest.raises(EngineSourceError):
        await resolve_engine_source(
            GateEngineConfig(
                enabled=True,
                mode="tag",
                repo_url="https://example.com/navapbc/ai-common-workflows.git",
                tag="v1.0.0",
                cache_root=str(tmp_path),
            ),
            runner=runner,
        )


async def test_zip_mode_extracts_and_pins_by_content_hash(tmp_path):
    source = tmp_path / "engine"
    source.mkdir()
    (source / "marker.txt").write_text("engine\n")
    zip_path = tmp_path / "engine.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.write(source / "marker.txt", "marker.txt")

    resolved = await resolve_engine_source(
        GateEngineConfig(
            enabled=True,
            mode="zip",
            zip_path=str(zip_path),
            cache_root=str(tmp_path / "cache"),
        ),
        runner=_FakeGit({}),
    )

    assert resolved is not None
    assert resolved.mode == "zip"
    assert resolved.version.startswith("sha256:")
    assert (Path(resolved.path) / "marker.txt").read_text() == "engine\n"


async def test_zip_mode_descends_into_a_single_wrapper_directory(tmp_path):
    zip_path = tmp_path / "wrapped.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("navapbc-ai-common-workflows-abc123/README.md", "top\n")
        archive.writestr(
            "navapbc-ai-common-workflows-abc123/engines/_common/CONTRACT.md", "contract\n"
        )

    resolved = await resolve_engine_source(
        GateEngineConfig(
            enabled=True,
            mode="zip",
            zip_path=str(zip_path),
            cache_root=str(tmp_path / "cache"),
        ),
        runner=_FakeGit({}),
    )

    assert resolved is not None
    assert Path(resolved.path).name == "navapbc-ai-common-workflows-abc123"
    assert (Path(resolved.path) / "engines" / "_common" / "CONTRACT.md").is_file()



async def test_unknown_mode_fails_fast():
    with pytest.raises(EngineSourceError):
        await resolve_engine_source(
            GateEngineConfig(enabled=True, mode="svn"), runner=_FakeGit({})
        )


def test_parse_disabled_config_is_inert():
    from services.orchestration.gates.engine_source import parse_gate_engine_config

    assert parse_gate_engine_config({"enabled": False, "mode": "tag"}).enabled is False
    assert parse_gate_engine_config({}).enabled is False


def test_parse_reads_the_mode_and_fields():
    from services.orchestration.gates.engine_source import parse_gate_engine_config

    config = parse_gate_engine_config(
        {
            "enabled": True,
            "mode": "tag",
            "repo_url": "https://example.com/x.git",
            "tag": "v2",
            "cache_root": "/cache",
        }
    )

    assert config.enabled is True
    assert config.mode == "tag"
    assert config.tag == "v2"


def test_parse_rejects_an_unknown_mode():
    from services.integration.credentials import ConfigError
    from services.orchestration.gates.engine_source import parse_gate_engine_config

    with pytest.raises(ConfigError):
        parse_gate_engine_config({"enabled": True, "mode": "svn"})


def test_entrypoints_default_to_the_reference_engine_layout():
    from services.orchestration.gates.engine_source import parse_gate_engine_config

    config = parse_gate_engine_config({"enabled": True, "mode": "local", "local_path": "/e"})

    assert config.review_entrypoint == "engines/security-compliance-review/harness/ai-pr-review"
    assert config.classifier_entrypoint == "engines/test-classifier/harness/ai-test-classifier"


def test_entrypoints_are_config_driven_for_another_engine():
    from services.orchestration.gates.engine_source import parse_gate_engine_config

    config = parse_gate_engine_config(
        {
            "enabled": True,
            "mode": "local",
            "local_path": "/e",
            "review_entrypoint": "bin/review",
            "classifier_entrypoint": "bin/classify",
        }
    )

    assert config.review_entrypoint == "bin/review"
    assert config.classifier_entrypoint == "bin/classify"


def test_load_is_inert_when_no_config_file_exists():
    from services.orchestration.gates.engine_source import load_gate_engine_configs

    configs = load_gate_engine_configs(environment="no-such-environment")

    assert configs == ()


def test_parse_reads_a_list_of_named_engines():
    from services.orchestration.gates.engine_source import parse_gate_engine_configs

    configs = parse_gate_engine_configs(
        {
            "engines": [
                {
                    "name": "ai-security-review",
                    "enabled": True,
                    "mode": "local",
                    "local_path": "/a",
                },
                {
                    "name": "sonarqube-review",
                    "enabled": False,
                    "mode": "local",
                    "local_path": "/b",
                },
            ]
        }
    )

    assert [c.name for c in configs] == ["ai-security-review", "sonarqube-review"]
    assert [c.enabled for c in configs] == [True, False]


def test_parse_rejects_a_duplicate_engine_name():
    from services.integration.credentials import ConfigError
    from services.orchestration.gates.engine_source import parse_gate_engine_configs

    with pytest.raises(ConfigError):
        parse_gate_engine_configs(
            {"engines": [{"name": "dup"}, {"name": "dup"}]}
        )


def test_parse_rejects_an_entry_without_a_name():
    from services.integration.credentials import ConfigError
    from services.orchestration.gates.engine_source import parse_gate_engine_configs

    with pytest.raises(ConfigError):
        parse_gate_engine_configs({"engines": [{"enabled": True}]})


def test_triage_engine_picks_the_first_enabled_with_a_classifier():
    from services.orchestration.gates.engine_source import (
        parse_gate_engine_configs,
        triage_engine,
    )

    configs = parse_gate_engine_configs(
        {
            "engines": [
                {"name": "review-only", "enabled": True, "classifier_entrypoint": ""},
                {"name": "full", "enabled": True, "classifier_entrypoint": "bin/classify"},
            ]
        }
    )

    picked = triage_engine(configs)
    assert picked is not None and picked.name == "full"


def test_triage_engine_is_none_when_no_enabled_classifier():
    from services.orchestration.gates.engine_source import (
        parse_gate_engine_configs,
        triage_engine,
    )

    configs = parse_gate_engine_configs(
        {"engines": [{"name": "review-only", "enabled": True, "classifier_entrypoint": ""}]}
    )

    assert triage_engine(configs) is None

