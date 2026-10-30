"""Per-repository project-checks config, resolved from config/<env>/checks.yaml.

A project declares how the platform runs its deterministic checks (lint, unit tests, SAST) with no
build-tool convention. Each repo names an environment, either a base ``image`` the command runs in
or a ``build`` Dockerfile that produces a test-capable image, and the ``command`` that runs the
checks. The command performs its own dependency setup the way the project's existing CI already
does. A repo may also declare ``fix_command``, the project's own formatter or auto-fixer, which the
repair stage runs in the same image before re-running ``command`` to verify the failure is gone.
A repo with no entry has no declared checks and the gate skips with a warning.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChecksConfig:
    repo: str
    image: str = ""
    build: str = ""
    command: str = ""
    fix_command: str = ""
    working_dir: str = "/workspace"
    timeout_minutes: int = 15


def parse_checks_config(repo: str, raw: dict) -> ChecksConfig:
    from services.integration.credentials import ConfigError

    image = str(raw.get("image", "")).strip()
    build = str(raw.get("build", "")).strip()
    command = str(raw.get("command", "")).strip()
    fix_command = str(raw.get("fix_command", "")).strip()
    if not command:
        raise ConfigError(f"checks entry {repo!r} needs a command")
    if not image and not build:
        raise ConfigError(f"checks entry {repo!r} needs an image or a build Dockerfile")
    if image and build:
        raise ConfigError(f"checks entry {repo!r} sets both image and build, choose one")
    working_dir = str(raw.get("working_dir", ChecksConfig.working_dir)).strip() or "/workspace"
    timeout = raw.get("timeout_minutes", ChecksConfig.timeout_minutes)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        raise ConfigError(f"checks entry {repo!r} timeout_minutes must be a positive integer")
    return ChecksConfig(
        repo=repo,
        image=image,
        build=build,
        command=command,
        fix_command=fix_command,
        working_dir=working_dir,
        timeout_minutes=timeout,
    )


def parse_checks_configs(raw: dict) -> dict[str, ChecksConfig]:
    from services.integration.credentials import ConfigError

    entries = raw.get("checks", {})
    if not isinstance(entries, dict):
        raise ConfigError("checks.checks must be a mapping keyed by repository")
    configs: dict[str, ChecksConfig] = {}
    for repo, entry in entries.items():
        if not isinstance(entry, dict):
            raise ConfigError(f"checks entry {repo!r} must be a mapping")
        configs[str(repo)] = parse_checks_config(str(repo), entry)
    return configs


def load_checks_configs(
    settings=None, *, environment: str | None = None
) -> dict[str, ChecksConfig]:
    from scripts.config import get_settings

    from services.integration.credentials import (
        _REPO_ROOT,
        load_env_config,
        require_current_config,
    )

    resolved = settings or get_settings()
    env = environment or resolved.environment
    if not (_REPO_ROOT / "config" / env / "checks.yaml").exists():
        return {}
    require_current_config("checks", environment=env)
    return parse_checks_configs(load_env_config("checks", environment=env))


def checks_for_repo(
    repo: str, settings=None, *, environment: str | None = None
) -> ChecksConfig | None:
    return load_checks_configs(settings, environment=environment).get(repo)


def required_checks_repos(
    settings=None, *, environment: str | None = None
) -> frozenset[str] | None:
    """Repositories with a checks entry when the backend requires CI, or None when it does not."""
    from scripts.config import get_settings

    from services.integration.github.factory import GITHUB, resolve_source_control_backend_name

    resolved = settings or get_settings()
    if resolve_source_control_backend_name(resolved) != GITHUB:
        return None
    return frozenset(load_checks_configs(resolved, environment=environment))
