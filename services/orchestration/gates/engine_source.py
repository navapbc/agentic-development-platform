"""Resolve an external engine source from config to a checkout by local, tag, or zip mode."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from services import process

LOCAL = "local"
TAG = "tag"
ZIP = "zip"
IMAGE = "image"
_MODES = frozenset({LOCAL, TAG, ZIP, IMAGE})

REVIEW_JSON = "review-json"
SARIF = "sarif"
_OUTPUT_FORMATS = frozenset({REVIEW_JSON, SARIF})

WARN = "warn"
ABORT = "abort"
_ON_UNAVAILABLE = frozenset({WARN, ABORT})


class EngineSourceError(Exception):
    """The engine source could not be resolved, so the run fails fast."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


GitRunner = Callable[[Sequence[str], str], Awaitable[CommandResult]]


@dataclass(frozen=True)
class GateEngineConfig:
    name: str = ""
    enabled: bool = False
    mode: str = LOCAL
    repo_url: str = ""
    tag: str = ""
    cache_root: str = ""
    local_path: str = ""
    zip_path: str = ""
    image: str = ""
    tool: str = "claude"
    review_model: str = ""
    triage_model: str = ""
    on_unavailable: str = "abort"
    max_output_tokens: int = 8192
    output_format: str = REVIEW_JSON
    review_entrypoint: str = "engines/security-compliance-review/harness/ai-pr-review"
    classifier_entrypoint: str = "engines/test-classifier/harness/ai-test-classifier"

    @property
    def needs_model(self) -> bool:
        return self.output_format == REVIEW_JSON


@dataclass(frozen=True)
class ResolvedEngineSource:
    path: str
    mode: str
    version: str
    tag: str = ""
    image: str = ""


async def run_git(args: Sequence[str], cwd: str) -> CommandResult:
    result = await process.run_async(
        ["git", *args], timeout=process.MEDIUM_COMMAND_TIMEOUT_SECONDS, cwd=cwd or None
    )
    return CommandResult(
        returncode=result.returncode,
        stdout=result.stdout.decode("utf-8", "replace"),
        stderr=result.stderr.decode("utf-8", "replace"),
    )


async def _git(runner: GitRunner, args: Sequence[str], cwd: str) -> str:
    result = await runner(args, cwd)
    if result.returncode != 0:
        raise EngineSourceError(
            f"git {' '.join(args)} failed with {result.returncode}: {result.stderr.strip()[:400]}"
        )
    return result.stdout.strip()


def _repo_slug(repo_url: str) -> str:
    tail = repo_url.rstrip("/").rsplit("/", 1)[-1]
    return tail[:-4] if tail.endswith(".git") else tail


async def _remote_tag_commit(runner: GitRunner, repo_url: str, tag: str) -> str:
    refs = [f"refs/tags/{tag}^{{}}", f"refs/tags/{tag}"]
    out = await _git(runner, ["ls-remote", "--", repo_url, *refs], "")
    if not out:
        raise EngineSourceError(f"engine tag {tag!r} not found at {repo_url}")
    lines = {line.split("\t")[1]: line.split("\t")[0] for line in out.splitlines() if "\t" in line}
    return lines.get(f"refs/tags/{tag}^{{}}") or lines.get(f"refs/tags/{tag}") or ""


async def _resolve_local(config: GateEngineConfig, runner: GitRunner) -> ResolvedEngineSource:
    path = Path(config.local_path)
    if not path.is_dir():
        raise EngineSourceError(f"engine local_path {config.local_path!r} is not a directory")
    commit = await _git(runner, ["rev-parse", "HEAD"], str(path))
    return ResolvedEngineSource(path=str(path), mode=LOCAL, version=commit)


async def _resolve_tag(config: GateEngineConfig, runner: GitRunner) -> ResolvedEngineSource:
    if not config.repo_url or not config.tag or not config.cache_root:
        raise EngineSourceError("engine tag mode needs repo_url, tag, and cache_root")
    cache_path = Path(config.cache_root) / _repo_slug(config.repo_url) / config.tag
    remote_commit = await _remote_tag_commit(runner, config.repo_url, config.tag)

    if cache_path.is_dir():
        cached = await _git(runner, ["rev-parse", "HEAD"], str(cache_path))
        if cached != remote_commit:
            await _git(
                runner,
                ["fetch", "--depth", "1", "origin", f"refs/tags/{config.tag}"],
                str(cache_path),
            )
            await _git(runner, ["checkout", "--force", "FETCH_HEAD"], str(cache_path))
    else:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        await _git(
            runner,
            [
                "clone",
                "--depth",
                "1",
                "--branch",
                config.tag,
                "--",
                config.repo_url,
                str(cache_path),
            ],
            "",
        )
    commit = await _git(runner, ["rev-parse", "HEAD"], str(cache_path))
    return ResolvedEngineSource(path=str(cache_path), mode=TAG, version=commit, tag=config.tag)


def _zip_hash(zip_path: Path) -> str:
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    return f"sha256:{digest}"


def _engine_root(extracted: Path) -> Path:
    entries = [child for child in extracted.iterdir() if not child.name.startswith("__MACOSX")]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return extracted


async def _resolve_zip(config: GateEngineConfig) -> ResolvedEngineSource:
    import zipfile

    zip_path = Path(config.zip_path)
    if not zip_path.is_file():
        raise EngineSourceError(f"engine zip_path {config.zip_path!r} is not a file")
    if not config.cache_root:
        raise EngineSourceError("engine zip mode needs cache_root")
    version = _zip_hash(zip_path)
    dest = Path(config.cache_root) / "zip" / version.split(":", 1)[1]
    if not dest.is_dir():
        dest.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(dest)
    return ResolvedEngineSource(path=str(_engine_root(dest)), mode=ZIP, version=version)


async def _resolve_image(config: GateEngineConfig) -> ResolvedEngineSource:
    if not config.image:
        raise EngineSourceError("engine image mode needs an image reference")
    return ResolvedEngineSource(path="", mode=IMAGE, version=config.image, image=config.image)


async def resolve_engine_source(
    config: GateEngineConfig, *, runner: GitRunner = run_git
) -> ResolvedEngineSource | None:
    if not config.enabled:
        return None
    if config.mode not in _MODES:
        raise EngineSourceError(f"engine mode must be one of {sorted(_MODES)}, got {config.mode!r}")
    if config.mode == LOCAL:
        return await _resolve_local(config, runner)
    if config.mode == TAG:
        return await _resolve_tag(config, runner)
    if config.mode == IMAGE:
        return await _resolve_image(config)
    return await _resolve_zip(config)


def parse_gate_engine_config(raw: dict) -> GateEngineConfig:
    from services.integration.credentials import ConfigError

    name = str(raw.get("name", ""))
    enabled = bool(raw.get("enabled", False))
    mode = str(raw.get("mode", LOCAL))
    if enabled and mode not in _MODES:
        raise ConfigError(f"gate_engine.mode must be one of {sorted(_MODES)}, got {mode!r}")
    if enabled and mode == IMAGE and not str(raw.get("image", "")):
        raise ConfigError("gate_engine.mode image needs an image reference")
    output_format = str(raw.get("output_format", REVIEW_JSON))
    if output_format not in _OUTPUT_FORMATS:
        raise ConfigError(
            f"gate_engine.output_format must be one of {sorted(_OUTPUT_FORMATS)}, "
            f"got {output_format!r}"
        )
    on_unavailable = str(raw.get("on_unavailable", ABORT))
    if enabled and on_unavailable not in _ON_UNAVAILABLE:
        raise ConfigError(
            f"gate_engine.on_unavailable must be one of {sorted(_ON_UNAVAILABLE)}, "
            f"got {on_unavailable!r}"
        )
    return GateEngineConfig(
        name=name,
        enabled=enabled,
        mode=mode,
        repo_url=str(raw.get("repo_url", "")),
        tag=str(raw.get("tag", "")),
        cache_root=str(raw.get("cache_root", "")),
        local_path=str(raw.get("local_path", "")),
        zip_path=str(raw.get("zip_path", "")),
        image=str(raw.get("image", "")),
        tool=str(raw.get("tool", "claude")),
        review_model=str(raw.get("review_model", "")),
        triage_model=str(raw.get("triage_model", "")),
        on_unavailable=on_unavailable,
        max_output_tokens=int(raw.get("max_output_tokens", 8192)),
        output_format=output_format,
        review_entrypoint=str(raw.get("review_entrypoint", GateEngineConfig.review_entrypoint)),
        classifier_entrypoint=str(
            raw.get("classifier_entrypoint", GateEngineConfig.classifier_entrypoint)
        ),
    )


def parse_gate_engine_configs(raw: dict) -> tuple[GateEngineConfig, ...]:
    from services.integration.credentials import ConfigError

    entries = raw.get("engines", [])
    if not isinstance(entries, list | tuple):
        raise ConfigError("gate_engine.engines must be a list")
    configs: list[GateEngineConfig] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or not str(entry.get("name", "")):
            raise ConfigError("each gate_engine entry must be a mapping with a name")
        config = parse_gate_engine_config(entry)
        if config.name in seen:
            raise ConfigError(f"gate_engine.engines has a duplicate name {config.name!r}")
        seen.add(config.name)
        configs.append(config)
    return tuple(configs)


def enabled_engines(configs: tuple[GateEngineConfig, ...]) -> tuple[GateEngineConfig, ...]:
    return tuple(config for config in configs if config.enabled)


def triage_engine(configs: tuple[GateEngineConfig, ...]) -> GateEngineConfig | None:
    for config in enabled_engines(configs):
        if config.classifier_entrypoint:
            return config
    return None


def load_gate_engine_configs(
    settings=None, *, environment: str | None = None
) -> tuple[GateEngineConfig, ...]:
    from scripts.config import get_settings

    from services.integration.credentials import (
        _REPO_ROOT,
        load_env_config,
        require_current_config,
    )

    resolved = settings or get_settings()
    env = environment or resolved.environment
    if not (_REPO_ROOT / "config" / env / "gate_engine.yaml").exists():
        return ()
    require_current_config("gate_engine", environment=env)
    return parse_gate_engine_configs(load_env_config("gate_engine", environment=env))
