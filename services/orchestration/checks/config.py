"""Per-repository project-checks config, resolved from config/local/checks.yaml.

A project declares how the platform runs its deterministic checks (lint, unit tests, SAST) with no
build-tool convention. Each repo names an environment, either a base ``image`` the command runs in
or a ``build`` Dockerfile that produces a test-capable image, and the ``command`` that runs the
checks. The command performs its own dependency setup the way the project's existing CI already
does. A repo may also declare ``fix_command``, the project's own formatter or auto-fixer, which the
repair stage runs in the same image before re-running ``command`` to verify the failure is gone.
A repo with no entry has no declared checks and the gate skips with a warning.
"""

from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class ChecksConfig:
    repo: str
    image: str = ""
    build: str = ""
    command: str = ""
    fix_command: str = ""
    working_dir: str = "/workspace"
    timeout_minutes: int = 15
    lock_command: str = ""
    lock_manifests: tuple[str, ...] = ()
    lockfiles: tuple[str, ...] = ()

    @property
    def lock_files(self) -> tuple[str, ...]:
        """Every file the lock command reads or writes, whose change runs the lock step."""
        return (*self.lock_manifests, *self.lockfiles)


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
    lock_command = str(raw.get("lock_command", "")).strip()
    names = {key: raw.get(key, []) for key in ("lock_manifests", "lockfiles")}
    for key, value in names.items():
        if not isinstance(value, list) or not all(isinstance(f, str) and f for f in value):
            raise ConfigError(f"checks entry {repo!r} {key} must be a list of file names")
    if lock_command and not (names["lock_manifests"] and names["lockfiles"]):
        raise ConfigError(
            f"checks entry {repo!r} sets lock_command, so it needs lock_manifests naming the "
            "manifests it reads and lockfiles naming the files it writes"
        )
    return ChecksConfig(
        repo=repo,
        image=image,
        build=build,
        command=command,
        fix_command=fix_command,
        working_dir=working_dir,
        timeout_minutes=timeout,
        lock_command=lock_command,
        lock_manifests=tuple(names["lock_manifests"]),
        lockfiles=tuple(names["lockfiles"]),
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


def load_checks_configs() -> dict[str, ChecksConfig]:

    from services.integration.credentials import (
        env_config_path,
        load_env_config,
        require_current_config,
    )

    if not env_config_path("checks").exists():
        return {}
    require_current_config("checks")
    return parse_checks_configs(load_env_config("checks"))


def checks_for_repo(repo: str, settings=None) -> ChecksConfig | None:
    """Return the repository's checks.yaml entry, or else its runtime profile's default checks.

    An entry that names no lock command takes its profile's, so a repository with its own checks
    command still gets its lockfile regenerated. An image written catalog:<name> is returned
    resolved through RUNTIME_IMAGES.
    """
    from services.integration.runtime_profiles import repository_profile, resolve_image

    config = load_checks_configs().get(repo)
    if config is None or not config.lock_command:
        profile = repository_profile(repo, settings)
        profiled = (
            parse_checks_config(repo, dict(profile.checks))
            if profile is not None and profile.checks is not None
            else None
        )
        if config is None:
            if profiled is None:
                return None
            config = profiled
        elif profiled is not None:
            config = replace(
                config,
                lock_command=profiled.lock_command,
                lock_manifests=profiled.lock_manifests,
                lockfiles=profiled.lockfiles,
            )
    if not config.image:
        return config
    return replace(config, image=resolve_image(config.image, settings))


def required_checks_repos(settings=None) -> frozenset[str] | None:
    """Repositories that satisfy the CI requirement, or None when no repository requires CI.

    A GitHub repository requires a checks entry, its own or its runtime profile's. A local
    repository has no CI to wait on, so it always satisfies the requirement.
    """
    from scripts.config import get_settings

    from services.integration.repositories import GITHUB, LOCAL, load_repository_registry

    resolved = settings or get_settings()
    registry = load_repository_registry(resolved)
    if not registry.names(GITHUB):
        return None
    from services.integration.runtime_profiles import load_profiles

    profiles = load_profiles()
    profiled = {
        repository.name
        for repository in registry.repositories.values()
        if repository.profile in profiles and profiles[repository.profile].checks is not None
    }
    checked = frozenset(load_checks_configs()) | profiled
    return checked | frozenset(registry.names(LOCAL))
