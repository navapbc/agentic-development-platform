"""Resolve an external engine source from config to a checkout or a published image."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
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

_PLATFORM_ROOT = Path(__file__).resolve().parents[3]
_ENGINE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


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
    review_entrypoint: str = ""
    classifier_entrypoint: str = ""

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
    if enabled and not str(raw.get("review_entrypoint", "")):
        raise ConfigError(f"gate_engine {name!r} needs a review_entrypoint")
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
        review_entrypoint=str(raw.get("review_entrypoint", "")),
        classifier_entrypoint=str(raw.get("classifier_entrypoint", "")),
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
        if config.enabled and config.mode == LOCAL and not _ENGINE_NAME.match(config.name):
            raise ConfigError(
                f"gate_engine name {config.name!r} names its directory in local mode, so it must "
                f"match {_ENGINE_NAME.pattern}"
            )
        if config.name in seen:
            raise ConfigError(f"gate_engine.engines has a duplicate name {config.name!r}")
        seen.add(config.name)
        configs.append(config)
    return tuple(configs)


def engine_host_path(config: GateEngineConfig, base: Path = _PLATFORM_ROOT) -> Path:
    """Return a local-mode engine's checkout on the host, resolving a relative path from base."""
    path = Path(config.local_path).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def locate_local_engines(
    configs: tuple[GateEngineConfig, ...], *, engine_root: str, in_image: bool
) -> tuple[GateEngineConfig, ...]:
    """Point each local-mode engine at the checkout this process reads.

    Inside the platform image, inv repos-sync copies the engine's working tree to
    <engine_root>/<name>. On the host the configured local_path is the checkout.
    """
    located = []
    for config in configs:
        if config.mode == LOCAL and config.local_path:
            path = (
                f"{engine_root.rstrip('/')}/{config.name}"
                if in_image
                else str(engine_host_path(config))
            )
            config = replace(config, local_path=path)
        located.append(config)
    return tuple(located)


def attach_published_images(
    configs: tuple[GateEngineConfig, ...], images: dict[str, str]
) -> tuple[GateEngineConfig, ...]:
    """Give each image-mode engine that names no image the one GATE_ENGINE_IMAGES publishes.

    A deployment publishes the engine images to its own registry, so gate_engine.yaml can name an
    engine without carrying an account-specific reference.
    """
    from services.integration.credentials import ConfigError

    attached = []
    for config in configs:
        if config.mode == IMAGE and not config.image:
            config = replace(config, image=images.get(config.name, ""))
            if config.enabled and not config.image:
                raise ConfigError(
                    f"gate_engine {config.name!r} runs in image mode, and neither its image nor "
                    "GATE_ENGINE_IMAGES names an image for it"
                )
        attached.append(config)
    return tuple(attached)


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
    from scripts.config import get_settings, in_platform_image

    from services.integration.credentials import (
        ConfigError,
        env_config_path,
        load_env_config,
        require_current_config,
    )

    resolved = settings or get_settings()
    env = environment or resolved.environment
    configs: tuple[GateEngineConfig, ...] = ()
    if env_config_path("gate_engine", environment=env).exists():
        require_current_config("gate_engine", environment=env)
        configs = parse_gate_engine_configs(load_env_config("gate_engine", environment=env))
    catalog = catalog_engines(resolved.gate_engine_images)
    reused = sorted({config.name for config in configs} & set(catalog_engine_names()))
    if reused:
        raise ConfigError(
            f"gate_engine.yaml redefines the catalog engines {', '.join(reused)}. Enable a catalog "
            "engine in gates.yaml by its name, or give a custom engine another name."
        )
    located = locate_local_engines(
        configs, engine_root=resolved.gate_engine_root, in_image=in_platform_image()
    )
    return attach_published_images(located, resolved.gate_engine_images) + catalog


def catalog_engine_names() -> tuple[str, ...]:
    return tuple(config.name for config in _catalog_engine_configs())


def _catalog_engine_configs() -> tuple[GateEngineConfig, ...]:
    from services.integration.credentials import load_platform_config

    return parse_gate_engine_configs(load_platform_config("gate-engines"))


def catalog_engines(images: dict[str, str]) -> tuple[GateEngineConfig, ...]:
    """Return the catalog engines whose image GATE_ENGINE_IMAGES names, with that image.

    An engine whose image this deployment has not published does not register, so enabling it
    in gates.yaml fails as an unregistered gate.
    """
    return tuple(
        replace(config, image=images[config.name])
        for config in _catalog_engine_configs()
        if images.get(config.name)
    )
