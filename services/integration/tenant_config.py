"""Load each tenant's config bundle, validate it, and pin one version for each run.

A tenant owns the config files that describe its work and its credentials, TENANT_FILES. Its
bundle holds any of them and layers over config/<environment>, so a file the bundle lacks is read
from the environment directory. Every other config file belongs to the platform and is read only
from the environment directory.

The bundle comes from the directory config/<environment>/tenants/<tenant>/, or from the prefix
tenants/<tenant>/ of TENANT_CONFIG_BUCKET when that is set. A configured process copies each
bundle it activates into a local store keyed by the bundle's identity, the version of each file,
and validates it there before activation. A bundle that fails validation is refused and the last
valid bundle stays active. A run records the identity of the bundle active at its start, and every
later activity of the run reads that identity's copy, so a run sees one config from start to end.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import shutil
import tempfile
import threading
from collections.abc import Callable, Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from services.integration.credentials import ConfigError

if TYPE_CHECKING:
    from scripts.config import Settings

log = logging.getLogger(__name__)

TENANT_FILES = (
    "repositories",
    "checks",
    "gates",
    "gate_engine",
    "registries",
    "github",
    "jira",
    "tickets",
    "profiles",
)
DIRECTORY = "directory"
S3 = "s3"
IDENTITY_FILE = "bundle.json"
STORE_KEEP = 20

_pinned: ContextVar[Path | None] = ContextVar("tenant_bundle_dir", default=None)
_pinned_identity: ContextVar[BundleIdentity | None] = ContextVar(
    "tenant_bundle_identity", default=None
)
_active: TenantConfig | None = None
_active_lock = threading.Lock()


class BundleUnavailableError(ConfigError):
    """The bundle a run pinned can no longer be read from its source."""


@dataclass(frozen=True)
class BundleIdentity:
    """Which version of each file a bundle holds, a content hash or an S3 version ID."""

    tenant: str
    source: str
    files: Mapping[str, str] = field(default_factory=dict)

    @property
    def id(self) -> str:
        digest = hashlib.sha256(f"{self.tenant}\0{self.source}".encode())
        for name, version in sorted(self.files.items()):
            digest.update(f"\0{name}={version}".encode())
        return digest.hexdigest()[:16]

    def to_manifest(self) -> dict[str, Any]:
        return {"id": self.id, "source": self.source, "files": dict(sorted(self.files.items()))}

    @classmethod
    def from_manifest(cls, tenant: str, raw: Mapping[str, Any]) -> BundleIdentity:
        return cls(tenant=tenant, source=str(raw["source"]), files=dict(raw.get("files") or {}))


@dataclass(frozen=True)
class Bundle:
    identity: BundleIdentity
    files: Mapping[str, str]


class BundleSource(Protocol):
    def latest(self) -> BundleIdentity: ...

    def fetch(self, identity: BundleIdentity) -> Bundle: ...


def _content_version(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def bundle_file_name(key: str) -> str | None:
    name = key.rsplit("/", 1)[-1]
    return name.removesuffix(".yaml") if name.endswith(".yaml") else None


class DirectorySource:
    """A bundle directory on disk. Only its current content can be fetched."""

    def __init__(self, path: Path, tenant: str) -> None:
        self.path = path
        self.tenant = tenant

    def _read(self) -> dict[str, str]:
        if not self.path.is_dir():
            return {}
        return {
            name: entry.read_text(encoding="utf-8")
            for entry in sorted(self.path.iterdir())
            if entry.is_file() and (name := bundle_file_name(entry.name)) is not None
        }

    def latest(self) -> BundleIdentity:
        files = {name: _content_version(text) for name, text in self._read().items()}
        return BundleIdentity(self.tenant, DIRECTORY, files)

    def fetch(self, identity: BundleIdentity) -> Bundle:
        files = self._read()
        current = BundleIdentity(
            self.tenant, DIRECTORY, {name: _content_version(text) for name, text in files.items()}
        )
        if current.id != identity.id:
            raise BundleUnavailableError(
                f"bundle {identity.id} of tenant {self.tenant} is no longer in {self.path}"
            )
        return Bundle(current, files)


class S3Source:
    """The tenants/<tenant>/ prefix of the versioned config bucket. Any version can be fetched."""

    def __init__(self, client: Any, bucket: str, tenant: str) -> None:
        self.client = client
        self.bucket = bucket
        self.tenant = tenant
        self.prefix = f"tenants/{tenant}/"

    def latest(self) -> BundleIdentity:
        files: dict[str, str] = {}
        paginator = self.client.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self.prefix):
            for version in page.get("Versions") or []:
                key = version["Key"]
                if not version.get("IsLatest") or "/" in key[len(self.prefix) :]:
                    continue
                if (name := bundle_file_name(key)) is not None:
                    files[name] = version["VersionId"]
        return BundleIdentity(self.tenant, S3, files)

    def fetch(self, identity: BundleIdentity) -> Bundle:
        files = {}
        for name, version in identity.files.items():
            try:
                response = self.client.get_object(
                    Bucket=self.bucket, Key=f"{self.prefix}{name}.yaml", VersionId=version
                )
            except Exception as exc:  # noqa: BLE001 - any read failure makes the bundle unavailable
                raise BundleUnavailableError(
                    f"{name}.yaml version {version} of tenant {self.tenant} is unreadable: {exc}"
                ) from exc
            files[name] = response["Body"].read().decode("utf-8")
        return Bundle(identity, files)


class BundleStore:
    """Local copies of the bundles a process has activated or pinned, one directory each."""

    def __init__(self, root: Path, tenant: str) -> None:
        self.root = root / tenant

    def path(self, identity: BundleIdentity) -> Path:
        return self.root / identity.id

    def get(self, identity: BundleIdentity) -> Path | None:
        path = self.path(identity)
        return path if (path / IDENTITY_FILE).is_file() else None

    def put(self, bundle: Bundle) -> Path:
        target = self.path(bundle.identity)
        if (target / IDENTITY_FILE).is_file():
            return target
        self.root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=self.root))
        for name, text in bundle.files.items():
            (staging / f"{name}.yaml").write_text(text, encoding="utf-8")
        (staging / IDENTITY_FILE).write_text(
            json.dumps({"tenant": bundle.identity.tenant, **bundle.identity.to_manifest()}),
            encoding="utf-8",
        )
        try:
            staging.rename(target)
        except OSError:
            shutil.rmtree(staging, ignore_errors=True)
            if not (target / IDENTITY_FILE).is_file():
                raise
        return target

    def prune(self, keep: int, protect: set[Path]) -> None:
        entries = sorted(
            (path for path in self.root.iterdir() if path.is_dir() and path not in protect),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for path in entries[keep:]:
            shutil.rmtree(path, ignore_errors=True)


@contextlib.contextmanager
def pinned(directory: Path | None, identity: BundleIdentity | None = None) -> Iterator[None]:
    """Read tenant files from directory, the copy of identity, for the duration of the block."""
    token = _pinned.set(directory)
    identity_token = _pinned_identity.set(identity)
    try:
        yield
    finally:
        _pinned_identity.reset(identity_token)
        _pinned.reset(token)


def current_identity() -> BundleIdentity | None:
    """Return the identity of the bundle this context reads, pinned or active."""
    if _pinned.get() is not None:
        return _pinned_identity.get()
    return _active.active if _active is not None else None


def tenant_bundle_dir(settings: Settings | None = None) -> Path | None:
    """Return the bundle directory tenant files are read from in this context, or None.

    A pinned run's copy wins, then the active bundle of a configured process. A process that
    never configured the tenant config, such as an operator command, reads the directory source
    of its tenant as it stands, without validation.
    """
    directory = _pinned.get()
    if directory is not None:
        return directory
    if _active is not None:
        return _active.active_path
    from scripts.config import get_settings

    resolved = settings or get_settings()
    if not resolved.tenant or resolved.tenant_config_bucket:
        return None
    from services.integration.credentials import env_config_dir

    return env_config_dir(resolved.environment) / "tenants" / resolved.tenant


def tenant_file(name: str) -> Path | None:
    """Return the bundle's copy of a tenant file, or None to read the environment's."""
    if name not in TENANT_FILES:
        return None
    directory = tenant_bundle_dir()
    if directory is None:
        return None
    path = directory / f"{name}.yaml"
    return path if path.is_file() else None


def _present(name: str, settings: Settings) -> bool:
    from services.integration.credentials import env_config_path

    return env_config_path(name, environment=settings.environment).is_file()


def _required_content(settings: Settings) -> list[str]:
    from services.integration.repositories import GITHUB, load_repository_registry
    from services.integration.tickets.backend import JIRA, REBAR
    from services.integration.tickets.factory import resolve_ticket_backend_name

    if not _present("repositories", settings):
        return ["repositories.yaml is required and names the tenant's repositories"]
    problems = []
    registry = load_repository_registry(settings)
    if not registry.names():
        problems.append("repositories.yaml names no repository")
    if not _present("gates", settings):
        problems.append("gates.yaml is required and names the gates a run evaluates")
    backend = resolve_ticket_backend_name(settings)
    if backend == REBAR and not _present("tickets", settings):
        problems.append("tickets.yaml is required with TICKET_BACKEND=rebar")
    if backend == JIRA and not _present("jira", settings):
        problems.append("jira.yaml is required with TICKET_BACKEND=jira")
    if registry.names(GITHUB) and not _present("github", settings):
        problems.append("github.yaml is required for a repository with backend github")
    from services.integration.image_policy import image_problems
    from services.integration.runtime_profiles import profile_problems

    problems.extend(profile_problems(settings))
    problems.extend(image_problems(settings))
    return problems


def _loaders(settings: Settings) -> list[tuple[str, Callable[[], object]]]:
    from services.integration.credentials import load_env_config
    from services.integration.github.credentials import build_github_credential_resolver
    from services.integration.jira.credentials import build_jira_credential_resolver
    from services.integration.tickets.rebar_config import (
        load_poll_config,
        load_rebar_backend_config,
    )
    from services.integration.tickets.store_config import load_tickets_store_config
    from services.integration.tickets.write_contract import load_write_back_scopes
    from services.orchestration.checks.config import load_checks_configs
    from services.orchestration.gates.config import load_gates_config
    from services.orchestration.gates.engine_source import load_gate_engine_configs
    from services.orchestration.gates.registry_mirrors import current as load_registries

    def present(name: str) -> bool:
        return _present(name, settings)

    loaders: list[tuple[str, Callable[[], object]]] = [
        ("checks", lambda: load_checks_configs(settings)),
        ("gate_engine", lambda: load_gate_engine_configs(settings)),
        ("registries", load_registries),
    ]
    if present("gates"):
        loaders.append(("gates", lambda: load_gates_config(settings)))
    if present("github"):
        loaders.append(("github", lambda: build_github_credential_resolver(settings)))
    if present("jira"):
        loaders.append(("jira", lambda: build_jira_credential_resolver(settings)))
    if present("tickets"):
        loaders.extend(
            [
                ("tickets", lambda: load_rebar_backend_config(settings)),
                ("tickets", lambda: load_poll_config(settings)),
                ("tickets", lambda: load_write_back_scopes(settings)),
            ]
        )
        if "store" in load_env_config("tickets", environment=settings.environment):
            loaders.append(("tickets", lambda: load_tickets_store_config(settings)))
    return loaders


def validate_bundle(directory: Path, settings: Settings) -> None:
    """Refuse a bundle whose effective config, layered over the environment's, is unusable.

    Every file the bundle holds must be a tenant file, every file the effective config holds
    must parse with the loader the platform uses, and the required content must be present.
    """
    unknown = sorted(
        entry.name
        for entry in directory.iterdir()
        if entry.is_file()
        and entry.name != IDENTITY_FILE
        and (bundle_file_name(entry.name) not in TENANT_FILES)
    )
    problems = [
        f"{name} is not a tenant config file. A bundle holds {', '.join(TENANT_FILES)}."
        for name in unknown
    ]
    with pinned(directory):
        try:
            problems.extend(_required_content(settings))
        except ConfigError as exc:
            problems.append(str(exc))
        for name, load in _loaders(settings):
            try:
                load()
            except (ConfigError, ValueError, KeyError, TypeError) as exc:
                problems.append(f"{name}.yaml: {exc}")
    if problems:
        reasons = "\n".join(f"- {problem}" for problem in dict.fromkeys(problems))
        raise ConfigError(f"the tenant config bundle is refused for these reasons:\n{reasons}")


def build_source(settings: Settings) -> BundleSource:
    from services.integration.credentials import env_config_dir

    if not settings.tenant:
        raise ConfigError("TENANT is not set. Every run belongs to a tenant.")
    if settings.tenant_config_bucket:
        from scripts.aws_clients import configure_aws_fips, s3_client

        configure_aws_fips(settings)
        client = s3_client(region=settings.aws_region)
        return S3Source(client, settings.tenant_config_bucket, settings.tenant)
    path = env_config_dir(settings.environment) / "tenants" / settings.tenant
    return DirectorySource(path, settings.tenant)


def _default_store_root() -> Path:
    return Path(os.environ.get("TMPDIR") or tempfile.gettempdir()) / "tenant-config"


Rejection = Callable[[BundleIdentity, str], None]


class TenantConfig:
    """The active bundle of one tenant in this process, refreshed from its source."""

    def __init__(
        self,
        settings: Settings,
        source: BundleSource,
        store: BundleStore,
        *,
        on_rejected: Rejection | None = None,
    ) -> None:
        self.settings = settings
        self.tenant = settings.tenant
        self.source = source
        self.store = store
        self.on_rejected = on_rejected
        self.active: BundleIdentity | None = None
        self.active_path: Path | None = None
        self._rejected: set[str] = set()
        self._lock = threading.Lock()

    def refresh(self) -> bool:
        """Activate the source's latest bundle when it is new and valid. Return whether it was."""
        with self._lock:
            latest = self.source.latest()
            if self.active is not None and latest.id == self.active.id:
                return False
            if latest.id in self._rejected:
                return False
            path = self.store.get(latest) or self.store.put(self.source.fetch(latest))
            try:
                validate_bundle(path, self.settings)
            except ConfigError as exc:
                self._rejected.add(latest.id)
                shutil.rmtree(path, ignore_errors=True)
                reason = str(exc)
                log.error("rejected bundle %s of tenant %s: %s", latest.id, self.tenant, reason)
                if self.on_rejected is not None:
                    self.on_rejected(latest, reason)
                if self.active is None:
                    raise
                return False
            self.active, self.active_path = latest, path
            self.store.prune(STORE_KEEP, {path})
            log.info("activated bundle %s of tenant %s", latest.id, self.tenant)
            return True

    def path_for(self, identity: BundleIdentity) -> Path:
        """Return the local copy of a bundle a run pinned, fetching it again when evicted."""
        if identity.tenant != self.tenant:
            raise ConfigError(
                f"the run pinned a bundle of tenant {identity.tenant}, and this worker serves "
                f"tenant {self.tenant}"
            )
        return self.store.get(identity) or self.store.put(self.source.fetch(identity))


def configure(
    settings: Settings,
    *,
    source: BundleSource | None = None,
    store_root: Path | None = None,
    on_rejected: Rejection | None = None,
) -> TenantConfig:
    """Make this process load its tenant's bundle, activating the first valid one now."""
    global _active
    config = TenantConfig(
        settings,
        source or build_source(settings),
        BundleStore(store_root or _default_store_root(), settings.tenant),
        on_rejected=on_rejected,
    )
    config.refresh()
    with _active_lock:
        _active = config
    return config


def active_tenant_config() -> TenantConfig | None:
    return _active


def reset() -> None:
    """Forget the configured tenant config, for a test or a process that reconfigures."""
    global _active
    with _active_lock:
        _active = None
