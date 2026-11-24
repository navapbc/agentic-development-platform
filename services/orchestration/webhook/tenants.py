"""Map each inbound delivery to the tenant that owns it, and hold each tenant's webhook secrets.

The webhook serves every tenant TENANTS names, or the one TENANT names, and refuses to start with
neither. It reads each tenant's repositories.yaml and jira.yaml from the tenant's config bundle,
the directory config/local/tenants/<tenant>/ or the prefix tenants/<tenant>/ of
TENANT_CONFIG_BUCKET, and a replaceable file the bundle lacks from the platform directory. Both
refresh once TENANT_CONFIG_REFRESH_SECONDS have passed, and a file that fails to read or parse
leaves the last valid copy in force.

A pull request delivery belongs to the tenant whose secret signs it, among the tenants whose
repositories.yaml lists its repository as a GitHub repository. Two tenants may list the same
repository, each with its own secret, and the delivery routes to the tenant whose secret verifies
it. A delivery that no claimant's secret verifies is refused, and a delivery that more than one
claimant's secret verifies is refused, because tenants that share a secret can forge each other's
deliveries. A Jira delivery names its tenant in its path, /webhook/jira/<tenant>, and is admitted
for the projects the allowlist of that tenant's jira.yaml names.

Each tenant signs its deliveries with its own secrets, GITHUB_WEBHOOK_SECRET_<TENANT> and
JIRA_WEBHOOK_SECRET_<TENANT>, so one tenant cannot forge the events of another. A tenant without
a secret of its own has its deliveries refused, even when it is the one tenant served. A tenant's
Slack requests verify with SLACK_SIGNING_SECRET_<TENANT>, and its action tokens with
ESCALATION_ACTION_KEY_<TENANT>, on the same terms. Two tenants on their own Slack apps that
declare the same Slack app ID, a tenant on its own app that declares the ID of the shared Slack
app, and two tenants that hold the same Slack signing secret have their Slack requests refused.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

import yaml

from services.integration.credentials import (
    ConfigError,
    config_version,
    current_config_version,
    env_config_dir,
)
from services.integration.integrations import IntegrationsConfig, parse_integrations_config
from services.integration.jira.credentials import parse_jira_config
from services.integration.repositories import GITHUB
from services.integration.tenant_config import (
    TENANT_REPLACEABLE_FILES,
    BundleSource,
    tenant_source,
)
from services.orchestration.webhook.pr_events import verify_signature
from services.repository_intelligence.tenants import TenantRegistries

if TYPE_CHECKING:
    from scripts.config import Settings


log = logging.getLogger(__name__)

GITHUB_SIGNING_ENV_PREFIX = "GITHUB_WEBHOOK_SECRET_"
JIRA_SIGNING_ENV_PREFIX = "JIRA_WEBHOOK_SECRET_"
JIRA_CONFIG = "jira"
INTEGRATIONS_CONFIG = "integrations"
MODE_RETRY_SECONDS = 5.0

SourceFactory = Callable[[str], BundleSource]


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
        self._projects: dict[str, frozenset[str]] = {}
        self._project_versions: dict[str, str] = {}
        self._projects_refreshed_at: dict[str, float] = {}
        self._modes: dict[str, IntegrationsConfig] = {}
        self._modes_due_at: dict[str, float] = {}
        self._lock = threading.Lock()
        self._refresh_locks: dict[tuple[str, str], threading.Lock] = {}

    def _refresh_lock(self, kind: str, tenant: str) -> threading.Lock:
        """Return the lock of one tenant's refresh, so a slow read stalls no other tenant."""
        with self._lock:
            return self._refresh_locks.setdefault((kind, tenant), threading.Lock())

    def tenants(self) -> list[str]:
        return self._registries.tenants()

    def _claimants(self) -> dict[str, tuple[str, ...]]:
        """Return each GitHub repository mapped to the sorted tenants that list it."""
        claims: dict[str, list[str]] = {}
        for tenant in self.tenants():
            registry = self._registries.registry(tenant)
            if registry is None:
                continue
            for name in registry.names(GITHUB):
                claims.setdefault(name, []).append(tenant)
        return {name: tuple(sorted(tenants)) for name, tenants in claims.items()}

    def claimants(self, repository: str) -> tuple[str, ...]:
        """Return the tenants that list the repository as a GitHub repository, sorted."""
        return self._claimants().get(repository, ())

    def repository_owners(self) -> dict[str, str]:
        """Return each GitHub repository that one tenant lists, mapped to that tenant."""
        return {
            name: tenants[0] for name, tenants in self._claimants().items() if len(tenants) == 1
        }

    def owner(self, repository: str) -> str | None:
        """Return the tenant when one lists the repository, or None when none or several do."""
        tenants = self.claimants(repository)
        return tenants[0] if len(tenants) == 1 else None

    def github_repositories(self) -> frozenset[str]:
        return frozenset(self._claimants())

    def jira_projects(self, tenant: str) -> frozenset[str]:
        """Return the projects the allowlist of the tenant's jira.yaml names."""
        with self._refresh_lock(JIRA_CONFIG, tenant):
            now = self._clock()
            last = self._projects_refreshed_at.get(tenant)
            if last is None or now - last >= self._interval:
                self._projects_refreshed_at[tenant] = now
                self._refresh_projects(tenant)
            return self._projects.get(tenant, frozenset())

    def _refresh_projects(self, tenant: str) -> None:
        try:
            text = self._jira_text(tenant)
            version = hashlib.sha256(text.encode()).hexdigest()
            if self._project_versions.get(tenant) == version:
                return
            projects = parse_jira_allowlist(text, tenant)
        except Exception as exc:  # noqa: BLE001
            log.error(
                "jira.yaml of tenant %s is refused, keeping the last valid one: %s",
                tenant,
                exc,
            )
            return
        self._projects[tenant] = projects
        self._project_versions[tenant] = version

    def integrations(self, tenant: str) -> IntegrationsConfig | None:
        """Return the delivery modes of the tenant's integrations.yaml, else the platform's.

        With neither file, every integration is polling. A file that fails to read or parse leaves
        the last valid modes in force. Until one read is valid the modes are unknown, so this
        returns None and the read is retried after MODE_RETRY_SECONDS.
        """
        with self._refresh_lock(INTEGRATIONS_CONFIG, tenant):
            now = self._clock()
            due = self._modes_due_at.get(tenant)
            if due is None or now >= due:
                valid = self._refresh_modes(tenant)
                known = valid or tenant in self._modes
                wait = self._interval if known else min(self._interval, MODE_RETRY_SECONDS)
                self._modes_due_at[tenant] = now + wait
            return self._modes.get(tenant)

    def mode(self, tenant: str, integration: str) -> str | None:
        """Return the tenant's delivery mode of the integration, or None while it is unknown."""
        config = self.integrations(tenant)
        return None if config is None else config.mode(integration)

    def _refresh_modes(self, tenant: str) -> bool:
        try:
            text = self.bundle_text(tenant, INTEGRATIONS_CONFIG)
            raw = yaml.safe_load(text) if text is not None else {}
            if not isinstance(raw, Mapping):
                raise ConfigError("integrations.yaml is not a mapping")
            if text is not None and config_version(raw) < current_config_version(
                INTEGRATIONS_CONFIG
            ):
                raise ConfigError(
                    f"integrations.yaml is version {config_version(raw)} and this release "
                    f"expects version {current_config_version(INTEGRATIONS_CONFIG)}"
                )
            self._modes[tenant] = parse_integrations_config(raw)
        except Exception as exc:  # noqa: BLE001
            log.error(
                "integrations.yaml of tenant %s is refused, keeping the last valid one: %s",
                tenant,
                exc,
            )
            return False
        return True

    def _jira_text(self, tenant: str) -> str:
        text = self.bundle_text(tenant, JIRA_CONFIG)
        if text is None:
            location = self.source(tenant).location
            raise ConfigError(f"tenant {tenant} has no jira.yaml in {location}")
        return text

    def source(self, tenant: str) -> BundleSource:
        source = self._sources.get(tenant)
        if source is None:
            source = self._sources[tenant] = self._source_factory(tenant)
        return source

    def bundle_text(self, tenant: str, name: str) -> str | None:
        """Return the tenant's bundle file, else the platform's copy of a replaceable file.

        This reads the bundle source, which can be S3, so an event loop calls it from a thread.
        """
        source = self.source(tenant)
        text = source.fetch_files(source.latest(), [name]).get(name)
        if text is not None or name not in TENANT_REPLACEABLE_FILES:
            return text
        path = env_config_dir() / f"{name}.yaml"
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None


@dataclass(frozen=True)
class RoutedRepositories:
    """Choose the tenant whose secret signs a PR-closed delivery, for the PR-closed handler.

    A repository several tenants list routes to the tenant whose secret verifies the delivery.
    A repository no tenant lists is verified with the secret of the one tenant served, so a
    single-tenant webhook refuses it as not allowlisted, as before tenants. With more tenants it
    has no such fallback and its delivery is refused as unsigned.
    """

    routing: TenantRouting
    secrets: Mapping[str, str]

    def select_tenant(self, repository: str | None, body: bytes, signature: str) -> str | None:
        claimants = self.routing.claimants(repository) if repository else ()
        matches = [
            tenant
            for tenant in claimants
            if verify_signature(self.secrets.get(tenant, ""), body, signature)
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            log.error(
                "tenants %s share a signing key for repository %s, so the delivery is refused",
                ", ".join(matches),
                repository,
            )
            return None
        if not claimants:
            served = self.routing.tenants()
            if len(served) == 1 and verify_signature(
                self.secrets.get(served[0], ""), body, signature
            ):
                return served[0]
        return None
