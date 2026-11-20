"""The repositories each tenant authorizes repository intelligence to serve and index.

Every tenant that TENANTS names contributes the repositories.yaml of its config bundle, read from
the tenant's bundle source, the directory config/<env>/tenants/<tenant>/ or the prefix
tenants/<tenant>/ of TENANT_CONFIG_BUCKET. A bundle without repositories.yaml contributes the
environment's config/<env>/repositories.yaml, as the bundle layering of a tenant worker does. Each
entry locates its checkout under /tenants/<tenant>/repositories, the read-only mount of that
tenant's checkouts, and its indexes under <index root>/<tenant>/.

A registry refreshes from its sources once TENANT_CONFIG_REFRESH_SECONDS have passed since the
last refresh. A repositories.yaml that fails to read or parse is logged and refused, and the last
valid registry of that tenant stays in force.

With TENANTS empty, the one tenant TENANT names is served. With neither set, the process runs in
platform mode. It serves the platform config/<env>/repositories.yaml under the tenant key "",
locates checkouts at REPOSITORY_ROOT, and keeps the index layout without a tenant directory, so a
single-tenant development setup keeps working. Platform mode accepts the REPO_INTELLIGENCE_TOKEN
of its own settings as the token of that key.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from services.integration.credentials import (
    ConfigError,
    config_version,
    current_config_version,
    env_config_dir,
)
from services.integration.repositories import (
    CONFIG_NAME,
    RepositoryRegistry,
    load_repository_registry,
    parse_repositories,
)
from services.integration.tenant_config import BundleSource, tenant_source
from services.orchestration.task_queues import tenant_names
from services.repository_intelligence.indexing.reindex import IndexRoots
from services.repository_intelligence.tenant_tokens import tenant_tokens

if TYPE_CHECKING:
    from scripts.config import Settings

log = logging.getLogger(__name__)

PLATFORM_TENANT = ""
TENANTS_MOUNT = Path("/tenants")

SourceFactory = Callable[[str], BundleSource]


@dataclass(frozen=True)
class IndexedRepository:
    tenant: str
    name: str
    root: Path
    ref: str | None
    index_roots: IndexRoots | None


def served_tenants(settings: Settings) -> list[str]:
    """Return the tenants TENANTS names, else the one TENANT names, else none for platform mode."""
    names = tenant_names(settings)
    if names:
        return names
    return [settings.tenant] if settings.tenant else []


def home_tenant(settings: Settings) -> str:
    """Return the tenant that owns the indexes built before indexes were kept per tenant."""
    tenants = served_tenants(settings)
    if settings.tenant in tenants:
        return settings.tenant
    return tenants[0] if tenants else PLATFORM_TENANT


def checkout_root(tenant: str) -> str:
    return str(TENANTS_MOUNT / tenant / "repositories")


def tenant_index_roots(tenant: str, base: IndexRoots | None = None) -> IndexRoots | None:
    """Return the index roots of a tenant, or None for the platform layout without a tenant."""
    if tenant == PLATFORM_TENANT:
        return None
    return (base or IndexRoots.default()).for_tenant(tenant)


def configured_tokens(
    settings: Settings, environ: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Return the bearer token of each served tenant, empty when no tenant has a token."""
    tenants = served_tenants(settings)
    if not tenants:
        token = settings.repo_intelligence_token.strip()
        return {PLATFORM_TENANT: token} if token else {}
    return tenant_tokens(tenants, settings.repo_intelligence_token_key, environ)


def parse_tenant_repositories(text: str, tenant: str) -> RepositoryRegistry:
    """Parse a tenant's repositories.yaml with every checkout under the tenant's mount."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"repositories.yaml of tenant {tenant} is not valid YAML: {exc}") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ConfigError(f"repositories.yaml of tenant {tenant} is not a mapping")
    expected = current_config_version(CONFIG_NAME)
    found = config_version(raw)
    if found < expected:
        raise ConfigError(
            f"repositories.yaml of tenant {tenant} is version {found} and this release "
            f"expects version {expected}"
        )
    return parse_repositories(raw, root=checkout_root(tenant))


class TenantRegistries:
    """The repository registry of every served tenant, refreshed from each tenant's bundle."""

    def __init__(
        self,
        settings: Settings,
        *,
        source_factory: SourceFactory | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._tenants = served_tenants(settings)
        self._source_factory = source_factory or (lambda tenant: tenant_source(settings, tenant))
        self._sources: dict[str, BundleSource] = {}
        self._registries: dict[str, RepositoryRegistry] = {}
        self._versions: dict[str, str] = {}
        self._clock = clock
        self._interval = float(settings.tenant_config_refresh_seconds)
        self._refreshed_at: float | None = None
        self._lock = threading.Lock()

    @property
    def platform_mode(self) -> bool:
        return not self._tenants

    def tenants(self) -> list[str]:
        return [PLATFORM_TENANT] if self.platform_mode else list(self._tenants)

    def refresh(self, *, force: bool = False) -> None:
        """Reload every tenant's registry when the refresh interval has passed or when forced."""
        with self._lock:
            now = self._clock()
            due = self._refreshed_at is None or now - self._refreshed_at >= self._interval
            if not (force or due):
                return
            self._refreshed_at = now
            if self.platform_mode:
                self._refresh_platform()
                return
            for tenant in self._tenants:
                self._refresh_tenant(tenant)

    def registry(self, tenant: str) -> RepositoryRegistry | None:
        self.refresh()
        return self._registries.get(tenant)

    def indexed(self) -> list[IndexedRepository]:
        """Return every repository a served tenant indexes, with its checkout and index roots."""
        self.refresh()
        entries: list[IndexedRepository] = []
        for tenant in self.tenants():
            registry = self._registries.get(tenant)
            if registry is None:
                continue
            for name, (location, ref) in registry.indexed().items():
                entries.append(
                    IndexedRepository(
                        tenant=tenant,
                        name=name,
                        root=Path(location),
                        ref=ref,
                        index_roots=tenant_index_roots(tenant),
                    )
                )
        return entries

    def _refresh_platform(self) -> None:
        try:
            self._registries[PLATFORM_TENANT] = load_repository_registry(self._settings)
        except ConfigError as exc:
            log.error("platform repositories.yaml is refused, keeping the last valid one: %s", exc)

    def _refresh_tenant(self, tenant: str) -> None:
        try:
            text = self._repositories_text(tenant)
            version = hashlib.sha256(text.encode()).hexdigest()
            if self._versions.get(tenant) == version:
                return
            registry = parse_tenant_repositories(text, tenant)
        except Exception as exc:  # noqa: BLE001
            log.error(
                "repositories.yaml of tenant %s is refused, keeping the last valid one: %s",
                tenant,
                exc,
            )
            return
        self._registries[tenant] = registry
        self._versions[tenant] = version
        log.info("loaded %d repositories of tenant %s", len(registry.names()), tenant)

    def _repositories_text(self, tenant: str) -> str:
        source = self._sources.get(tenant)
        if source is None:
            source = self._sources[tenant] = self._source_factory(tenant)
        bundle = source.fetch(source.latest())
        text = bundle.files.get(CONFIG_NAME)
        if text is not None:
            return text
        path = env_config_dir(self._settings.environment) / f"{CONFIG_NAME}.yaml"
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise ConfigError(
                f"tenant {tenant} has no repositories.yaml in its bundle or in {path.parent}"
            ) from exc


def migrate_home_indexes(
    registries: TenantRegistries, settings: Settings, base: IndexRoots | None = None
) -> list[Path]:
    """Move each index <root>/<repo>@<commit> of a home tenant repository under the tenant.

    The index directory becomes <root>/<tenant>/<repo>@<commit>, so the indexes built before
    indexes were kept per tenant are reused. An index whose target exists stays where it is.
    Repository intelligence and the index worker share the index volume and both migrate on
    start, so a directory the other process already moved is skipped.
    """
    tenant = home_tenant(settings)
    if registries.platform_mode or tenant == PLATFORM_TENANT:
        return []
    registry = registries.registry(tenant)
    if registry is None:
        return []
    roots = base or IndexRoots.default()
    moved: list[Path] = []
    for root in (roots.scip, roots.zoekt):
        if not root.is_dir():
            continue
        for name in sorted(registry.names()):
            for entry in sorted(root.glob(f"{name}@*")):
                target = root / tenant / entry.name
                if not entry.is_dir() or target.exists():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.rename(entry, target)
                except FileNotFoundError:
                    continue
                log.info("moved index %s to %s", entry, target)
                moved.append(target)
    return moved
