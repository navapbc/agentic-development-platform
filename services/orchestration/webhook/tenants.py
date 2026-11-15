"""Map each inbound delivery to the tenant that owns it, and hold each tenant's webhook secrets.

The webhook serves every tenant TENANTS names, or the one TENANT names, or, with neither, the
platform under the tenant key "". It reads each tenant's repositories.yaml and jira.yaml from the
tenant's config bundle, the directory config/<env>/tenants/<tenant>/ or the prefix
tenants/<tenant>/ of TENANT_CONFIG_BUCKET, and falls back to the environment's copy as a tenant
worker does. Both refresh once TENANT_CONFIG_REFRESH_SECONDS have passed, and a file that fails to
read or parse leaves the last valid copy in force.

A pull request delivery belongs to the tenant whose repositories.yaml lists its repository as a
GitHub repository. A repository that two tenants list is refused when the registries load. The
tenant that held it before the conflict keeps it, and a repository no tenant held before routes
to no tenant until the conflict is resolved. A Jira delivery names its tenant in its path,
/webhook/jira/<tenant>, and is admitted for the projects the allowlist of that tenant's jira.yaml
names.

Each tenant signs its deliveries with its own secrets, GITHUB_WEBHOOK_SECRET_<TENANT> and
JIRA_WEBHOOK_SECRET_<TENANT>, so one tenant cannot forge the events of another. When the webhook
serves one tenant, that tenant without a secret of its own uses GITHUB_WEBHOOK_SECRET and
JIRA_WEBHOOK_SECRET, the secrets of a single-tenant deployment.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

import yaml

from services.integration.credentials import (
    ConfigError,
    config_version,
    current_config_version,
    env_config_dir,
)
from services.integration.jira.credentials import allowed_projects, parse_jira_config
from services.integration.repositories import GITHUB
from services.integration.tenant_config import BundleSource, tenant_source
from services.repository_intelligence.tenants import PLATFORM_TENANT, TenantRegistries

if TYPE_CHECKING:
    from scripts.config import Settings

log = logging.getLogger(__name__)

GITHUB_SIGNING_ENV_PREFIX = "GITHUB_WEBHOOK_SECRET_"
JIRA_SIGNING_ENV_PREFIX = "JIRA_WEBHOOK_SECRET_"
JIRA_CONFIG = "jira"

SourceFactory = Callable[[str], BundleSource]


def secret_env_name(prefix: str, tenant: str) -> str:
    return f"{prefix}{tenant.upper()}"


def tenant_secrets(
    tenants: Sequence[str],
    prefix: str,
    platform_secret: str,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return the signing secret of each tenant that has one.

    A tenant's own variable wins. The platform secret signs for a tenant only when it is the one
    tenant served, because tenants that share a secret can forge each other's deliveries.
    """
    values = os.environ if environ is None else environ
    secrets: dict[str, str] = {}
    for tenant in tenants:
        own = values.get(secret_env_name(prefix, tenant), "").strip() if tenant else ""
        if own:
            secrets[tenant] = own
        elif len(tenants) == 1 and platform_secret:
            secrets[tenant] = platform_secret
    return secrets


def parse_jira_allowlist(text: str, tenant: str) -> frozenset[str]:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"jira.yaml of tenant {tenant} is not valid YAML: {exc}") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ConfigError(f"jira.yaml of tenant {tenant} is not a mapping")
    expected = current_config_version(JIRA_CONFIG)
    found = config_version(raw)
    if found < expected:
        raise ConfigError(
            f"jira.yaml of tenant {tenant} is version {found} and this release expects "
            f"version {expected}"
        )
    return frozenset(parse_jira_config(raw).allowlist)


class TenantRouting:
    """The GitHub repositories and Jira projects of every served tenant, refreshed from bundles."""

    def __init__(
        self,
        settings: Settings,
        *,
        registries: TenantRegistries | None = None,
        source_factory: SourceFactory | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._registries = registries or TenantRegistries(settings, source_factory=source_factory)
        self._source_factory = source_factory or (lambda tenant: tenant_source(settings, tenant))
        self._sources: dict[str, BundleSource] = {}
        self._clock = clock
        self._interval = float(settings.tenant_config_refresh_seconds)
        self._owners: dict[str, str] = {}
        self._conflicts: dict[str, tuple[str, ...]] = {}
        self._projects: dict[str, frozenset[str]] = {}
        self._project_versions: dict[str, str] = {}
        self._projects_refreshed_at: dict[str, float] = {}
        self._lock = threading.Lock()

    def tenants(self) -> list[str]:
        return self._registries.tenants()

    def repository_owners(self) -> dict[str, str]:
        """Return the tenant that owns each GitHub repository, leaving out refused repositories."""
        claims: dict[str, list[str]] = {}
        for tenant in self.tenants():
            registry = self._registries.registry(tenant)
            if registry is None:
                continue
            for name in registry.names(GITHUB):
                claims.setdefault(name, []).append(tenant)
        with self._lock:
            owners: dict[str, str] = {}
            conflicts: dict[str, tuple[str, ...]] = {}
            for name, claimants in claims.items():
                if len(claimants) == 1:
                    owners[name] = claimants[0]
                    continue
                conflicts[name] = tuple(claimants)
                previous = self._owners.get(name)
                if previous in claimants:
                    owners[name] = previous
            for name, claimants in sorted(conflicts.items()):
                if self._conflicts.get(name) != claimants:
                    log.error(
                        "repository %s is listed by tenants %s, so it is refused and stays with %s",
                        name,
                        ", ".join(claimants),
                        owners.get(name) or "no tenant",
                    )
            self._owners, self._conflicts = owners, conflicts
            return dict(owners)

    def owner(self, repository: str) -> str | None:
        return self.repository_owners().get(repository)

    def github_repositories(self) -> frozenset[str]:
        return frozenset(self.repository_owners())

    def jira_projects(self, tenant: str) -> frozenset[str]:
        """Return the projects the allowlist of the tenant's jira.yaml names."""
        with self._lock:
            now = self._clock()
            last = self._projects_refreshed_at.get(tenant)
            if last is None or now - last >= self._interval:
                self._projects_refreshed_at[tenant] = now
                self._refresh_projects(tenant)
            return self._projects.get(tenant, frozenset())

    def _refresh_projects(self, tenant: str) -> None:
        try:
            if tenant == PLATFORM_TENANT:
                self._projects[tenant] = frozenset(allowed_projects(self._settings))
                return
            text = self._jira_text(tenant)
            version = hashlib.sha256(text.encode()).hexdigest()
            if self._project_versions.get(tenant) == version:
                return
            projects = parse_jira_allowlist(text, tenant)
        except Exception as exc:  # noqa: BLE001
            log.error(
                "jira.yaml of tenant %s is refused, keeping the last valid one: %s",
                tenant or "platform",
                exc,
            )
            return
        self._projects[tenant] = projects
        self._project_versions[tenant] = version

    def _jira_text(self, tenant: str) -> str:
        source = self._sources.get(tenant)
        if source is None:
            source = self._sources[tenant] = self._source_factory(tenant)
        text = source.fetch(source.latest()).files.get(JIRA_CONFIG)
        if text is not None:
            return text
        path = env_config_dir(self._settings.environment) / f"{JIRA_CONFIG}.yaml"
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise ConfigError(
                f"tenant {tenant} has no jira.yaml in its bundle or in {path.parent}"
            ) from exc


@dataclass(frozen=True)
class RoutedRepositories:
    """The owning tenant and signing secret of each repository, for the PR-closed handler.

    A repository no tenant owns is verified with the secret of the one tenant served, so a
    single-tenant webhook refuses it as not allowlisted, as before tenants. With more tenants it
    has no secret and its delivery is refused as unsigned.
    """

    routing: TenantRouting
    secrets: Mapping[str, str]

    def owner(self, repository: str) -> str | None:
        return self.routing.owner(repository)

    def signing_secret(self, repository: str | None) -> str:
        tenant = self.owner(repository) if repository else None
        if tenant is None:
            tenants = self.routing.tenants()
            tenant = tenants[0] if len(tenants) == 1 else None
        return self.secrets.get(tenant, "") if tenant is not None else ""
