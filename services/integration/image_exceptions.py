"""Read and write the platform's and each tenant's image_exceptions.yaml.

The two files keep one layout locally and in AWS. Without TENANT_CONFIG_BUCKET the platform file
is config/<environment>/image_exceptions.yaml and a tenant's file is
config/<environment>/tenants/<tenant>/image_exceptions.yaml. With it set, the platform file is
platform/image_exceptions.yaml in that bucket, read by version ID and cached per version, and a
tenant's file is tenants/<tenant>/image_exceptions.yaml, one more file of the tenant's bundle.

Each loader enforces its file's scope, and `image_policy.parse_exceptions` checks every entry.
The pull gate honours the platform's entries and the run tenant's entries, and a file it cannot
use contributes no entry. A write replaces a file only when no other writer replaced it since it
was read, through a conditional PutObject on the ETag it read, and retries on a conflict.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import yaml

from services.integration.credentials import ConfigError, current_config_version
from services.integration.image_policy import (
    EXCEPTIONS,
    PLATFORM_SCOPE,
    TENANT_SCOPE,
    FindingException,
    ImagePolicy,
    load_policy,
    parse_exceptions,
    parse_policy,
    require_scope,
)

if TYPE_CHECKING:
    from scripts.config import Settings

log = logging.getLogger(__name__)

FILE_NAME = f"{EXCEPTIONS}.yaml"
PLATFORM_PREFIX = "platform/"
PLATFORM_KEY = f"{PLATFORM_PREFIX}{FILE_NAME}"
POLICY_KEY = f"{PLATFORM_PREFIX}image_policy.yaml"
TENANTS_PREFIX = "tenants/"
CONTENT_TYPE = "application/yaml"
WRITE_ATTEMPTS = 5
_CACHE_LIMIT = 64
_MISSING_CODES = {"404", "NoSuchKey", "NotFound"}
_CONFLICT_CODES = {"412", "409", "PreconditionFailed", "ConditionalRequestConflict"}


class MissingExceptionsError(ConfigError):
    """The platform exceptions file does not exist."""


class UnreadableFileError(ConfigError):
    """A config file exists, or may exist, but cannot be read."""


class StaleWriteError(ConfigError):
    """Another writer replaced the file between this writer's read and its write."""


@dataclass(frozen=True)
class Snapshot:
    """One read of a file. `text` is None when the file is absent."""

    location: str
    text: str | None
    version: str = ""
    etag: str = ""

    @property
    def exists(self) -> bool:
        return self.text is not None


class ExceptionsFile(Protocol):
    @property
    def location(self) -> str: ...

    def read(self) -> Snapshot: ...

    def write(self, text: str, base: Snapshot) -> str: ...


def _content_version(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


class LocalFile:
    """A file on disk. A write replaces it only when it still holds what `base` read."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @property
    def location(self) -> str:
        return str(self.path)

    def read(self) -> Snapshot:
        if not self.path.is_file():
            return Snapshot(self.location, None)
        try:
            text = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise UnreadableFileError(f"{self.location} cannot be read: {exc}") from exc
        version = _content_version(text)
        return Snapshot(self.location, text, version, version)

    def write(self, text: str, base: Snapshot) -> str:
        if self.read().text != base.text:
            raise StaleWriteError(f"{self.location} changed after it was read")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        staging = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        staging.write_text(text, encoding="utf-8")
        os.replace(staging, self.path)
        return _content_version(text)


_texts: dict[tuple[str, str, str], str] = {}
_texts_lock = threading.Lock()


def _error_code(exc: BaseException) -> str:
    response = getattr(exc, "response", None) or {}
    return str((response.get("Error") or {}).get("Code") or "")


class S3File:
    """One key of the versioned config bucket, whose text is cached per version ID."""

    def __init__(self, client: Any, bucket: str, key: str) -> None:
        self.client = client
        self.bucket = bucket
        self.key = key

    @property
    def location(self) -> str:
        return f"s3://{self.bucket}/{self.key}"

    def read(self) -> Snapshot:
        try:
            head = self.client.head_object(Bucket=self.bucket, Key=self.key)
        except Exception as exc:  # noqa: BLE001
            if _error_code(exc) in _MISSING_CODES:
                return Snapshot(self.location, None)
            raise UnreadableFileError(f"{self.location} cannot be read: {exc}") from exc
        version = str(head.get("VersionId") or "")
        etag = str(head.get("ETag") or "")
        cache_key = (self.bucket, self.key, version)
        with _texts_lock:
            text = _texts.get(cache_key) if version else None
        if text is None:
            versioned = {"VersionId": version} if version else {}
            try:
                response = self.client.get_object(Bucket=self.bucket, Key=self.key, **versioned)
                text = response["Body"].read().decode("utf-8")
            except Exception as exc:  # noqa: BLE001
                raise UnreadableFileError(f"{self.location} cannot be read: {exc}") from exc
            etag = str(response.get("ETag") or etag)
            if version:
                with _texts_lock:
                    if len(_texts) >= _CACHE_LIMIT:
                        _texts.clear()
                    _texts[cache_key] = text
        return Snapshot(self.location, text, version, etag)

    def write(self, text: str, base: Snapshot) -> str:
        condition = {"IfMatch": base.etag} if base.exists else {"IfNoneMatch": "*"}
        try:
            response = self.client.put_object(
                Bucket=self.bucket,
                Key=self.key,
                Body=text.encode("utf-8"),
                ContentType=CONTENT_TYPE,
                **condition,
            )
        except Exception as exc:
            if _error_code(exc) in _CONFLICT_CODES:
                raise StaleWriteError(f"{self.location} changed after it was read") from exc
            raise
        return str(response.get("VersionId") or "")


def _client(settings: Settings) -> Any:
    from scripts.aws_clients import configure_aws_fips, s3_client

    configure_aws_fips(settings)
    return s3_client(region=settings.aws_region)


def platform_file(settings: Settings, client: Any = None) -> ExceptionsFile:
    """Return the platform file the settings select, local or in the config bucket."""
    if settings.tenant_config_bucket:
        return S3File(client or _client(settings), settings.tenant_config_bucket, PLATFORM_KEY)
    from services.integration.credentials import env_config_dir

    return LocalFile(env_config_dir(settings.environment) / FILE_NAME)


def tenant_file(settings: Settings, tenant: str = "") -> LocalFile:
    """Return a tenant's local file. In AWS a tenant publishes its file with its bundle."""
    from services.integration.credentials import env_config_dir

    name = tenant or settings.tenant
    if not name:
        raise ConfigError("name the tenant with --tenant or TENANT")
    return LocalFile(env_config_dir(settings.environment) / "tenants" / name / FILE_NAME)


def tenant_files(settings: Settings, client: Any = None) -> list[tuple[str, ExceptionsFile]]:
    """Return every tenant's file, each tenant with the file it holds."""
    if not settings.tenant_config_bucket:
        from services.integration.credentials import env_config_dir

        root = env_config_dir(settings.environment) / "tenants"
        if not root.is_dir():
            return []
        return [
            (directory.name, LocalFile(directory / FILE_NAME))
            for directory in sorted(root.iterdir())
            if (directory / FILE_NAME).is_file()
        ]
    client = client or _client(settings)
    bucket = settings.tenant_config_bucket
    found = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=TENANTS_PREFIX):
        for item in page.get("Contents") or []:
            parts = str(item["Key"]).split("/")
            if len(parts) == 3 and parts[2] == FILE_NAME:
                found.append((parts[1], S3File(client, bucket, str(item["Key"]))))
    return sorted(found, key=lambda pair: pair[0])


def parse_text(text: str | None, scope: str, location: str) -> tuple[FindingException, ...]:
    """Parse a file's text with the shared parser. An absent file holds no entry."""
    if text is None:
        return ()
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{location} is not YAML: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ConfigError(f"{location} is not a mapping")
    return parse_exceptions(raw, scope=scope, source=location)


def load_file(path: str | Path, scope: str = PLATFORM_SCOPE) -> tuple[FindingException, ...]:
    """Parse one local file, raising ConfigError when it is absent or invalid."""
    snapshot = LocalFile(Path(path)).read()
    if not snapshot.exists:
        raise ConfigError(f"{path} does not exist")
    return parse_text(snapshot.text, scope, snapshot.location)


def scope_policy(settings: Settings, client: Any = None) -> ImagePolicy:
    """Return the image policy that bounds each file's scope.

    With TENANT_CONFIG_BUCKET set it is platform/image_policy.yaml, which the composition root
    writes beside the platform file, so a process without the environment's config, such as a
    GitHub workflow, checks scope against the deployment's policy. Otherwise, or when that object
    is absent, it is config/<environment>/image_policy.yaml.
    """
    if settings.tenant_config_bucket:
        snapshot = S3File(
            client or _client(settings), settings.tenant_config_bucket, POLICY_KEY
        ).read()
        if snapshot.exists:
            try:
                raw = yaml.safe_load(snapshot.text or "") or {}
            except yaml.YAMLError as exc:
                raise ConfigError(f"{snapshot.location} is not YAML: {exc}") from exc
            if not isinstance(raw, Mapping):
                raise ConfigError(f"{snapshot.location} is not a mapping")
            return parse_policy(raw)
    return load_policy(settings.environment)


def read_platform(
    settings: Settings,
    *,
    policy: ImagePolicy | None = None,
    client: Any = None,
    path_override: str = "",
) -> tuple[Snapshot, tuple[FindingException, ...]]:
    """Read the platform file and check every entry and its scope, or raise ConfigError."""
    source = LocalFile(Path(path_override)) if path_override else platform_file(settings, client)
    snapshot = source.read()
    if not snapshot.exists:
        raise MissingExceptionsError(f"the platform exceptions file {source.location} is missing")
    exceptions = parse_text(snapshot.text, PLATFORM_SCOPE, snapshot.location)
    if exceptions:
        require_scope(
            exceptions,
            PLATFORM_SCOPE,
            policy or scope_policy(settings, client),
            source=snapshot.location,
        )
    return snapshot, exceptions


def tenant_mirrors(directory: Path) -> dict[str, str]:
    """Return the mirrors of the registries.yaml a tenant's bundle holds itself."""
    path = directory / "registries.yaml"
    if not path.is_file():
        return {}
    from services.orchestration.gates.registry_mirrors import RegistryConfigError, parse

    try:
        return dict(parse(yaml.safe_load(path.read_text(encoding="utf-8")) or {}).mirrors)
    except (RegistryConfigError, yaml.YAMLError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def read_tenant(
    settings: Settings, policy: ImagePolicy, *, directory: Path | None = None
) -> tuple[FindingException, ...]:
    """Read the tenant's file from its bundle and check its scope, or raise ConfigError.

    The bundle is the directory given, or the one this context reads. A bundle without the file
    holds no entry.
    """
    if directory is None:
        from services.integration.tenant_config import tenant_bundle_dir

        directory = tenant_bundle_dir(settings)
    if directory is None:
        return ()
    snapshot = LocalFile(directory / FILE_NAME).read()
    exceptions = parse_text(snapshot.text, TENANT_SCOPE, snapshot.location)
    if exceptions:
        require_scope(
            exceptions,
            TENANT_SCOPE,
            policy,
            tenant=settings.tenant,
            mirrors=tenant_mirrors(directory),
            source=snapshot.location,
        )
    return exceptions


def gate_exceptions(environment: str) -> tuple[FindingException, ...]:
    """Return the platform's and the run tenant's entries for the pull gate, each in scope.

    A file that is missing, unreadable, or invalid contributes no entry, so the gate refuses
    every finding that file would have accepted.
    """
    from scripts.config import get_settings

    settings = get_settings()
    if settings.environment != environment:
        settings = settings.model_copy(update={"environment": environment})
    policy = load_policy(environment)
    loaders: list[tuple[str, Callable[[], tuple[FindingException, ...]]]] = [
        ("platform", lambda: read_platform(settings, policy=policy)[1]),
        (f"tenant {settings.tenant}", lambda: read_tenant(settings, policy)),
    ]
    entries: list[FindingException] = []
    for owner, load in loaders:
        try:
            entries.extend(load())
        except MissingExceptionsError as exc:
            log.warning("%s, so the pull gate accepts no platform finding", exc)
        except Exception as exc:  # noqa: BLE001
            log.error("the %s image exceptions are ignored, so they accept nothing: %s", owner, exc)
    return tuple(entries)


def platform_exceptions(
    settings: Settings, path_override: str = ""
) -> tuple[FindingException, ...]:
    """Return the platform's entries for the pre-push scan, ImageScanner's `exceptions=`.

    `path_override` names a local file to read in place of the file TENANT_CONFIG_BUCKET
    selects. A missing or unreadable file holds no entry, so every finding blocks the push. An
    invalid or out-of-scope entry raises ConfigError naming the entry.
    """
    try:
        return read_platform(settings, path_override=path_override)[1]
    except (MissingExceptionsError, UnreadableFileError) as exc:
        log.warning("%s, so the scan accepts no finding", exc)
        return ()


def exceptions_setup_problems(settings: Settings, client: Any = None) -> list[str]:
    """Return why the platform exceptions file is unusable, naming the path or key looked for."""
    source = platform_file(settings, client)
    try:
        read_platform(settings, client=client)
    except MissingExceptionsError as exc:
        if settings.tenant_config_bucket:
            remedy = "Apply the tenant-config-store module, which creates it with no entry."
        else:
            remedy = "Copy config/local/image_exceptions.yaml.example to create it."
        return [f"{exc}, so the pull gate accepts no platform finding. {remedy}"]
    except ConfigError as exc:
        return [
            f"the platform exceptions file {source.location} is unusable, so the pull gate "
            f"accepts no platform finding. {exc}"
        ]
    return []


def _header(text: str | None) -> str:
    lines = []
    for line in (text or "").splitlines(keepends=True):
        if line.strip() and not line.lstrip().startswith("#"):
            break
        lines.append(line)
    return "".join(lines)


def dump(exceptions: Iterable[FindingException], header: str = "") -> str:
    body = yaml.safe_dump(
        {
            "config_version": current_config_version(EXCEPTIONS),
            "exceptions": [exception.to_entry() for exception in exceptions],
        },
        sort_keys=False,
        default_flow_style=False,
    )
    return f"{header}{body}"


@dataclass(frozen=True)
class Update:
    location: str
    version: str
    before: tuple[FindingException, ...]
    after: tuple[FindingException, ...]
    written: bool


Change = Callable[[tuple[FindingException, ...]], tuple[FindingException, ...]]
Check = Callable[[tuple[FindingException, ...]], None]


def update(
    source: ExceptionsFile,
    change: Change,
    *,
    scope: str,
    check: Check | None = None,
    attempts: int = WRITE_ATTEMPTS,
) -> Update:
    """Apply `change` to the file's entries and write the result, retrying on a conflict.

    Every attempt reads the file again and applies the change to what it read, so a concurrent
    writer's entries survive. The result is parsed and checked before it is written.
    """
    for _ in range(attempts):
        snapshot = source.read()
        before = parse_text(snapshot.text, scope, snapshot.location)
        after = change(before)
        if snapshot.exists and after == before:
            return Update(snapshot.location, snapshot.version, before, after, False)
        text = dump(after, _header(snapshot.text))
        parsed = parse_text(text, scope, snapshot.location)
        if check is not None:
            check(parsed)
        try:
            version = source.write(text, snapshot)
        except StaleWriteError:
            continue
        return Update(snapshot.location, version, before, parsed, True)
    raise StaleWriteError(
        f"{source.location} kept changing, and {attempts} attempts to write it failed"
    )


def scope_check(
    scope: str,
    policy: ImagePolicy,
    *,
    tenant: str = "",
    mirrors: Mapping[str, str] | None = None,
    source: str = FILE_NAME,
) -> Check:
    def check(exceptions: tuple[FindingException, ...]) -> None:
        require_scope(exceptions, scope, policy, tenant=tenant, mirrors=mirrors, source=source)

    return check
