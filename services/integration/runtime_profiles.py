"""Runtime profiles: the containers and gates a repository's runs use, by its tech stack.

A profile names the sandbox image the coding agent runs in, the default checks of a repository
that has no checks.yaml entry, and gate overrides applied over the tenant's gates.yaml. The
platform ships catalog profiles in config/platform/runtime-profiles.yaml, and a tenant adds its
own in its profiles.yaml, under names the catalog does not use. A repository names its profile in
repositories.yaml.

A sandbox image written catalog:<name> resolves through RUNTIME_IMAGES, which maps each catalog
image the deployment publishes to its reference, except catalog:agent-server-python-node, which is
the platform's own OPENHANDS_AGENT_SERVER_IMAGE. Any other value is used as written.

The run's repository is bound for the duration of each activity, so the gate, checks, and
sandbox lookups resolve the profile of the repository the run works on.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from services.integration.credentials import (
    ConfigError,
    env_config_path,
    load_env_config,
    load_platform_config,
    require_current_config,
)

if TYPE_CHECKING:
    from scripts.config import Settings

CATALOG_IMAGE_PREFIX = "catalog:"
# The platform's own agent-server image, OPENHANDS_AGENT_SERVER_IMAGE, carries Python and Node.
PLATFORM_SANDBOX_IMAGE = "agent-server-python-node"
_PROFILE_FIELDS = frozenset({"description", "sandbox_image", "checks", "gates"})

_current_repository: ContextVar[str] = ContextVar("run_repository", default="")


@dataclass(frozen=True)
class RuntimeProfile:
    name: str
    description: str = ""
    sandbox_image: str = ""
    checks: Mapping[str, Any] | None = None
    gates: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    catalog: bool = False


def parse_profiles(
    raw: Mapping[str, Any], *, catalog: bool, source: str
) -> dict[str, RuntimeProfile]:
    entries = raw.get("profiles") or {}
    if not isinstance(entries, Mapping):
        raise ConfigError(f"{source} profiles must be a mapping of profile name to profile")
    profiles = {}
    for name, entry in entries.items():
        key = str(name)
        if not isinstance(entry, Mapping):
            raise ConfigError(f"{source} profile {key!r} must be a mapping")
        unknown = sorted(set(entry) - _PROFILE_FIELDS)
        if unknown:
            raise ConfigError(f"{source} profile {key!r} has unknown fields {', '.join(unknown)}")
        checks = entry.get("checks")
        if checks is not None and not isinstance(checks, Mapping):
            raise ConfigError(f"{source} profile {key!r} checks must be a mapping")
        gates = entry.get("gates") or []
        if not isinstance(gates, list) or not all(isinstance(gate, Mapping) for gate in gates):
            raise ConfigError(f"{source} profile {key!r} gates must be a list of gate entries")
        profiles[key] = RuntimeProfile(
            name=key,
            description=str(entry.get("description") or ""),
            sandbox_image=str(entry.get("sandbox_image") or ""),
            checks=dict(checks) if checks is not None else None,
            gates=tuple(dict(gate) for gate in gates),
            catalog=catalog,
        )
    return profiles


def catalog_profiles() -> dict[str, RuntimeProfile]:
    return parse_profiles(
        load_platform_config("runtime-profiles"),
        catalog=True,
        source="config/platform/runtime-profiles.yaml",
    )


def load_profiles(settings: Settings | None = None) -> dict[str, RuntimeProfile]:
    """Return the catalog and tenant profiles, refusing a tenant profile with a catalog name."""
    from scripts.config import get_settings

    resolved = settings or get_settings()
    profiles = catalog_profiles()
    if env_config_path("profiles", environment=resolved.environment).exists():
        require_current_config("profiles", environment=resolved.environment)
        tenant = parse_profiles(
            load_env_config("profiles", environment=resolved.environment),
            catalog=False,
            source="profiles.yaml",
        )
        reused = sorted(set(tenant) & set(profiles))
        if reused:
            raise ConfigError(
                f"profiles.yaml reuses the catalog profile names {', '.join(reused)}. Give a "
                "tenant profile its own name."
            )
        profiles.update(tenant)
    return profiles


def repository_profile(repo: str, settings: Settings | None = None) -> RuntimeProfile | None:
    """Return the profile repositories.yaml assigns to repo, or None when it names none."""
    from services.integration.repositories import load_repository_registry

    if not repo:
        return None
    repository = load_repository_registry(settings).get(repo)
    if repository is None or not repository.profile:
        return None
    profiles = load_profiles(settings)
    if repository.profile not in profiles:
        raise ConfigError(
            f"repositories.yaml assigns repository {repo} the profile {repository.profile!r}, "
            "which neither the catalog nor profiles.yaml defines"
        )
    return profiles[repository.profile]


def resolve_image(reference: str, settings: Settings | None = None) -> str:
    """Resolve catalog:<name> through RUNTIME_IMAGES, and return any other reference as written."""
    from scripts.config import get_settings

    if not reference.startswith(CATALOG_IMAGE_PREFIX):
        return reference
    name = reference.removeprefix(CATALOG_IMAGE_PREFIX)
    resolved = settings or get_settings()
    if name == PLATFORM_SANDBOX_IMAGE:
        return resolved.openhands_agent_server_image
    image = resolved.runtime_images.get(name, "")
    if not image:
        raise ConfigError(
            f"the catalog image {name} is not published in this deployment, so RUNTIME_IMAGES "
            "names no reference for it"
        )
    return image


def sandbox_image(repo: str, settings: Settings | None = None) -> str:
    """Return the sandbox image of repo's profile, or the platform's when it has none."""
    from scripts.config import get_settings

    resolved = settings or get_settings()
    profile = repository_profile(repo, resolved)
    if profile is None or not profile.sandbox_image:
        return resolved.openhands_agent_server_image
    return resolve_image(profile.sandbox_image, resolved)


@contextlib.contextmanager
def bound_repository(repo: str) -> Iterator[None]:
    """Resolve profile lookups against repo for the duration of the block."""
    token = _current_repository.set(repo)
    try:
        yield
    finally:
        _current_repository.reset(token)


def current_repository() -> str:
    return _current_repository.get()


def current_profile(settings: Settings | None = None) -> RuntimeProfile | None:
    return repository_profile(current_repository(), settings)


def profile_problems(settings: Settings) -> list[str]:
    """Return why the profiles, and each repository's profile, cannot serve a run."""
    from services.integration.repositories import load_repository_registry
    from services.orchestration.checks.config import parse_checks_config
    from services.orchestration.gates.config import load_gates_config

    try:
        profiles = load_profiles(settings)
    except ConfigError as exc:
        return [str(exc)]
    problems = []
    checked_gates: set[str] = set()
    for repository in load_repository_registry(settings).repositories.values():
        if not repository.profile:
            continue
        profile = profiles.get(repository.profile)
        if profile is None:
            problems.append(
                f"repository {repository.name} names the profile {repository.profile!r}, which "
                "neither the catalog nor profiles.yaml defines"
            )
            continue
        try:
            if profile.sandbox_image:
                resolve_image(profile.sandbox_image, settings)
            if profile.checks is not None:
                parse_checks_config(repository.name, dict(profile.checks))
            if profile.name not in checked_gates:
                checked_gates.add(profile.name)
                with bound_repository(repository.name):
                    load_gates_config(settings)
        except ConfigError as exc:
            problems.append(f"profile {profile.name} of repository {repository.name}: {exc}")
    return problems
