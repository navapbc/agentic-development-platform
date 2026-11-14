"""The images a tenant pod may run, and the findings the platform accepts in them.

`config/<environment>/image_policy.yaml` belongs to the platform and is never part of a tenant's
bundle. In AWS the composition root generates it. It names the repositories each kind of image
may come from:

- `catalog`, the platform's own repositories, signed with `signatures.platform_profile`
- `tenant_prefix`, the tenant's ECR prefix, where `{tenant}` stands for the tenant's name, signed
  with the tenant's profile in `signatures.tenant_profiles`
- `caches`, the pull-through cache prefixes, which carry no signature
- `registries`, any other prefix the deployment admits, such as a workstation registry

`platform_registries` names the registry hosts whose repositories only those entries admit. A
mirror the tenant's registries.yaml names is admitted too, unless it points into one of those
hosts outside the entries, so a tenant cannot reach another tenant's prefix through a mirror.

An entry ending in `/` admits every repository under it, and any other entry admits exactly that
repository. Every image reference must carry an `@sha256:` digest.

An image_exceptions.yaml lists scan findings accepted for one image digest each, with a reason,
the day the entry was written, and an expiry at most EXCEPTION_WINDOW_DAYS after that day. Two
levels exist, and each accepts findings only within its owner's scope:

- the platform file, for the images the platform publishes or defines, the `catalog` entries and
  every repository under a `platform_registries` host outside the tenant prefix. Its entries also
  name the requester and the approver.
- a tenant's file, one more file of the tenant's bundle, for the images under the tenant's prefix
  or under a mirror of the tenant's own registries.yaml outside the platform's scope

`services.integration.image_exceptions` reads and writes both files.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from services.integration.credentials import (
    ConfigError,
    config_version,
    current_config_version,
    env_config_path,
    load_env_config,
    require_current_config,
)

POLICY = "image_policy"
EXCEPTIONS = "image_exceptions"
CATALOG = "catalog"
TENANT = "tenant"
CACHE = "cache"
REGISTRY = "registry"
INSPECTOR = "inspector"
OFF = "off"
SCAN_GATES = (INSPECTOR, OFF)
SEVERITIES = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
DOCKER_HUB = "docker.io"
PLATFORM_SCOPE = "platform"
TENANT_SCOPE = "tenant"
EXCEPTION_SCOPES = (PLATFORM_SCOPE, TENANT_SCOPE)
EXCEPTION_WINDOW_DAYS = 30
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True)
class ImageReference:
    registry: str
    repository: str
    tag: str = ""
    digest: str = ""

    @property
    def name(self) -> str:
        return f"{self.registry}/{self.repository}"

    def __str__(self) -> str:
        tag = f":{self.tag}" if self.tag else ""
        digest = f"@{self.digest}" if self.digest else ""
        return f"{self.name}{tag}{digest}"


def parse_reference(text: str) -> ImageReference:
    """Parse an image reference, naming Docker Hub and its library the way the pull does."""
    reference = text.strip()
    if not reference or any(character.isspace() for character in reference):
        raise ConfigError(f"{text!r} is not an image reference")
    remainder, _, digest = reference.partition("@")
    if digest and not _DIGEST.match(digest):
        raise ConfigError(f"{text!r} carries a digest that is not sha256:<64 hex digits>")
    first, slash, rest = remainder.partition("/")
    if slash and ("." in first or ":" in first or first == "localhost"):
        registry, path = first, rest
    else:
        registry, path = DOCKER_HUB, remainder
    tag = ""
    if ":" in path.rsplit("/", 1)[-1]:
        path, tag = path.rsplit(":", 1)
    if registry == DOCKER_HUB and "/" not in path:
        path = f"library/{path}"
    if not path:
        raise ConfigError(f"{text!r} names no repository")
    return ImageReference(registry=registry, repository=path, tag=tag, digest=digest)


@dataclass(frozen=True)
class SignaturePolicy:
    verification: bool = False
    platform_profile: str = ""
    tenant_profiles: Mapping[str, str] = field(default_factory=dict)

    def profile_for(self, kind: str, tenant: str) -> str:
        if kind == CATALOG:
            return self.platform_profile
        if kind == TENANT:
            return self.tenant_profiles.get(tenant, "")
        return ""


@dataclass(frozen=True)
class ImagePolicy:
    catalog: tuple[str, ...] = ()
    tenant_prefix: str = ""
    caches: tuple[str, ...] = ()
    registries: tuple[str, ...] = ()
    platform_registries: tuple[str, ...] = ()
    scan_gate: str = OFF
    severity: str = "HIGH"
    signatures: SignaturePolicy = field(default_factory=SignaturePolicy)

    def entries(self, tenant: str) -> list[tuple[str, str]]:
        """Return each admitting entry with its kind, the tenant's prefix expanded."""
        entries = [(entry, CATALOG) for entry in self.catalog]
        if self.tenant_prefix and tenant:
            entries.append((self.tenant_prefix.replace("{tenant}", tenant), TENANT))
        entries += [(entry, CACHE) for entry in self.caches]
        entries += [(entry, REGISTRY) for entry in self.registries]
        return entries

    def blocking(self) -> tuple[str, ...]:
        """Return the severities at and above the one that blocks an image."""
        return SEVERITIES[SEVERITIES.index(self.severity) :]


def _matches(name: str, entry: str) -> bool:
    return name.startswith(entry) if entry.endswith("/") else name == entry


def _strings(raw: Any, key: str) -> tuple[str, ...]:
    values = raw.get(key) or []
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ConfigError(f"image_policy.yaml {key} must be a list of strings")
    return tuple(value.strip() for value in values if value.strip())


def parse_policy(raw: Mapping[str, Any]) -> ImagePolicy:
    scan = raw.get("scan") or {}
    gate = str(scan.get("gate") or OFF)
    if gate not in SCAN_GATES:
        raise ConfigError(f"image_policy.yaml scan.gate must be one of {', '.join(SCAN_GATES)}")
    severity = str(scan.get("severity") or "HIGH").upper()
    if severity not in SEVERITIES:
        raise ConfigError(f"image_policy.yaml scan.severity must be one of {', '.join(SEVERITIES)}")
    signed = raw.get("signatures") or {}
    tenant_profiles = signed.get("tenant_profiles") or {}
    if not isinstance(tenant_profiles, dict):
        raise ConfigError("image_policy.yaml signatures.tenant_profiles maps each tenant to an ARN")
    signatures = SignaturePolicy(
        verification=bool(signed.get("verification", False)),
        platform_profile=str(signed.get("platform_profile") or ""),
        tenant_profiles={str(k): str(v) for k, v in tenant_profiles.items()},
    )
    if signatures.verification and not signatures.platform_profile:
        raise ConfigError("image_policy.yaml signatures.verification needs platform_profile")
    tenant_prefix = str(raw.get("tenant_prefix") or "").strip()
    if tenant_prefix and ("{tenant}" not in tenant_prefix or not tenant_prefix.endswith("/")):
        raise ConfigError("image_policy.yaml tenant_prefix must contain {tenant} and end with /")
    return ImagePolicy(
        catalog=_strings(raw, "catalog"),
        tenant_prefix=tenant_prefix,
        caches=_strings(raw, "caches"),
        registries=_strings(raw, "registries"),
        platform_registries=_strings(raw, "platform_registries"),
        scan_gate=gate,
        severity=severity,
        signatures=signatures,
    )


def load_policy(environment: str, root: Path | None = None) -> ImagePolicy:
    """Load the environment's image policy. Every environment has one."""
    if not env_config_path(POLICY, environment=environment, root=root).exists():
        raise ConfigError(
            f"config/{environment}/image_policy.yaml is required. It names the registries a "
            "tenant pod may pull from. Copy image_policy.yaml.example to create it."
        )
    require_current_config(POLICY, environment=environment, root=root)
    return parse_policy(load_env_config(POLICY, environment=environment, root=root))


@dataclass(frozen=True)
class FindingException:
    image: str
    finding: str
    reason: str
    expires: date
    created: date | None = None
    requester: str = ""
    approver: str = ""

    def describe(self) -> str:
        return f"{self.finding} in {self.image}"

    def to_entry(self) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "image": self.image,
            "finding": self.finding,
            "reason": self.reason,
            "created": self.created,
            "expires": self.expires,
            "requester": self.requester,
            "approver": self.approver,
        }
        return {key: value for key, value in entry.items() if value}


def _required_fields(scope: str) -> tuple[str, ...]:
    common = ("image", "finding", "reason", "created", "expires")
    return (*common, "requester", "approver") if scope == PLATFORM_SCOPE else common


def _date(value: Any, context: str, key: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ConfigError(f"{context} {key} must be a YYYY-MM-DD date") from exc


def parse_exception(
    entry: Any, *, scope: str = PLATFORM_SCOPE, context: str = "image_exceptions.yaml entry"
) -> FindingException:
    """Parse one entry, which needs every field its scope requires and a bounded expiry."""
    if scope not in EXCEPTION_SCOPES:
        raise ConfigError(f"the exception scope must be one of {', '.join(EXCEPTION_SCOPES)}")
    if not isinstance(entry, Mapping):
        raise ConfigError(f"{context} must be a mapping")
    missing = [key for key in _required_fields(scope) if not str(entry.get(key) or "").strip()]
    if missing:
        raise ConfigError(f"{context} needs {', '.join(missing)}")
    image = parse_reference(str(entry["image"]))
    if not image.digest:
        raise ConfigError(f"{context} image must carry an @sha256: digest")
    created = _date(entry["created"], context, "created")
    expires = _date(entry["expires"], context, "expires")
    if expires < created:
        raise ConfigError(f"{context} expires on {expires}, before it was created on {created}")
    if (expires - created).days > EXCEPTION_WINDOW_DAYS:
        raise ConfigError(
            f"{context} expires on {expires}, more than {EXCEPTION_WINDOW_DAYS} days after it "
            f"was created on {created}"
        )
    return FindingException(
        image=f"{image.name}@{image.digest}",
        finding=str(entry["finding"]).strip(),
        reason=str(entry["reason"]).strip(),
        expires=expires,
        created=created,
        requester=str(entry.get("requester") or "").strip(),
        approver=str(entry.get("approver") or "").strip(),
    )


def parse_exceptions(
    raw: Mapping[str, Any], *, scope: str = PLATFORM_SCOPE, source: str = "image_exceptions.yaml"
) -> tuple[FindingException, ...]:
    """Parse an image_exceptions.yaml of either scope, checking its version and every entry.

    The scanner, the pull gate, the review, and every writer share this parser. It checks the
    fields only, and `require_scope` checks which images the entries may name.
    """
    expected = current_config_version(EXCEPTIONS)
    if config_version(raw) != expected:
        raise ConfigError(f"{source} config_version must be {expected}")
    entries = raw.get("exceptions") or []
    if not isinstance(entries, list):
        raise ConfigError(f"{source} exceptions must be a list")
    parsed: list[FindingException] = []
    seen: set[tuple[str, str]] = set()
    for index, entry in enumerate(entries):
        context = f"{source} exceptions[{index}]"
        exception = parse_exception(entry, scope=scope, context=context)
        key = (exception.image, exception.finding)
        if key in seen:
            raise ConfigError(f"{context} accepts {exception.describe()} a second time")
        seen.add(key)
        parsed.append(exception)
    return tuple(parsed)


def require_new_entry(exception: FindingException, today: date) -> None:
    """Refuse an entry being written unless it was created today and expires within the window."""
    if exception.created != today:
        raise ConfigError(f"a new exception must be created on {today}, the day it is written")
    if exception.expires < today:
        raise ConfigError(f"the exception for {exception.describe()} expires before {today}")


def _under(name: str, prefix: str) -> bool:
    stem = prefix.rstrip("/")
    return name == stem or name.startswith(f"{stem}/")


def _any_tenant_prefix(policy: ImagePolicy) -> re.Pattern[str] | None:
    if not policy.tenant_prefix:
        return None
    head, _, tail = policy.tenant_prefix.partition("{tenant}")
    return re.compile(f"^{re.escape(head)}[^/]+{re.escape(tail)}")


def in_platform_scope(name: str, policy: ImagePolicy) -> bool:
    """Return whether the platform file may accept findings in the repository `name`."""
    if any(_matches(name, entry) for entry in policy.catalog):
        return True
    if name.split("/", 1)[0] not in policy.platform_registries:
        return False
    tenant_prefix = _any_tenant_prefix(policy)
    return tenant_prefix is None or tenant_prefix.match(name) is None


def in_tenant_scope(
    name: str, policy: ImagePolicy, tenant: str, mirrors: Mapping[str, str] | None = None
) -> bool:
    """Return whether a tenant's file may accept findings in the repository `name`.

    `mirrors` are the tenant's own registries.yaml mirrors. A mirror into a platform registry
    host widens nothing, so a tenant reaches only its own prefix inside the platform's hosts.
    """
    if in_platform_scope(name, policy):
        return False
    own_prefix = policy.tenant_prefix.replace("{tenant}", tenant) if tenant else ""
    if own_prefix and _matches(name, own_prefix):
        return True
    for upstream, location in (mirrors or {}).items():
        if location.split("/", 1)[0] in policy.platform_registries:
            continue
        if _under(name, upstream) or _under(name, location):
            return True
    return False


def require_scope(
    exceptions: Iterable[FindingException],
    scope: str,
    policy: ImagePolicy,
    *,
    tenant: str = "",
    mirrors: Mapping[str, str] | None = None,
    source: str = "image_exceptions.yaml",
) -> None:
    """Refuse the first entry that names an image outside the scope of the file's owner."""
    for index, exception in enumerate(exceptions):
        name = exception.image.partition("@")[0]
        if scope == PLATFORM_SCOPE:
            if in_platform_scope(name, policy):
                continue
            hosts = ", ".join(policy.platform_registries) or "no host"
            raise ConfigError(
                f"{source} exceptions[{index}] accepts {exception.describe()}, outside the "
                f"platform's scope. The platform file accepts findings only in the catalog and "
                f"in repositories under {hosts} outside the tenant prefix."
            )
        if in_tenant_scope(name, policy, tenant, mirrors):
            continue
        prefix = policy.tenant_prefix.replace("{tenant}", tenant) or "(none)"
        raise ConfigError(
            f"{source} exceptions[{index}] accepts {exception.describe()}, outside the scope of "
            f"tenant {tenant or '(none)'}. A tenant file accepts findings only in images under "
            f"{prefix} or under a mirror of the tenant's own registries.yaml outside the "
            "platform's registries."
        )


def load_exceptions(environment: str) -> tuple[FindingException, ...]:
    """Return the entries the pull gate honours, the platform's and the run tenant's.

    Each file's entries are limited to its scope. A missing or unusable file contributes no
    entry, so every finding it would have accepted blocks.
    """
    from services.integration.image_exceptions import gate_exceptions

    return gate_exceptions(environment)


def accepted(
    exceptions: Iterable[FindingException], image: ImageReference, finding: str, today: date
) -> FindingException | None:
    """Return the unexpired exception that accepts the finding in the image, if any."""
    key = f"{image.name}@{image.digest}"
    for exception in exceptions:
        if exception.image == key and exception.finding == finding and exception.expires >= today:
            return exception
    return None


@dataclass(frozen=True)
class AdmittedImage:
    """An image the policy admits, with the reference the pull resolves to and its kind."""

    requested: ImageReference
    resolved: ImageReference
    kind: str


def _mirrored(reference: ImageReference, mirrors: Mapping[str, str]) -> ImageReference:
    """Return the reference a registries.conf mirror sends the pull to, or the reference itself."""
    name = reference.name
    for upstream in sorted(mirrors, key=len, reverse=True):
        prefix = upstream.rstrip("/")
        if name == prefix or name.startswith(f"{prefix}/"):
            location = mirrors[upstream].rstrip("/") + name[len(prefix) :]
            registry, _, repository = location.partition("/")
            return ImageReference(registry, repository, reference.tag, reference.digest)
    return reference


def admits_mirror(location: str, policy: ImagePolicy, tenant: str) -> bool:
    """Return whether a registries.yaml mirror location may serve pulls under the policy."""
    name = location.rstrip("/")
    for entry, _ in policy.entries(tenant):
        if (entry.endswith("/") and f"{name}/".startswith(entry)) or name == entry.rstrip("/"):
            return True
    return name.split("/", 1)[0] not in policy.platform_registries


def admit(
    text: str,
    policy: ImagePolicy,
    tenant: str,
    mirrors: Mapping[str, str] | None = None,
) -> AdmittedImage:
    """Admit an image reference or raise ConfigError naming why the policy refuses it."""
    reference = parse_reference(text)
    if not reference.digest:
        raise ConfigError(f"the image {text} carries no @sha256: digest, which every image needs")
    resolved = _mirrored(reference, mirrors or {})
    for entry, kind in policy.entries(tenant):
        if _matches(resolved.name, entry):
            return AdmittedImage(reference, resolved, kind)
    if resolved is not reference and resolved.registry not in policy.platform_registries:
        return AdmittedImage(reference, resolved, REGISTRY)
    raise ConfigError(
        f"the image {text} comes from {resolved.name}, which image_policy.yaml does not admit "
        f"for tenant {tenant or '(none)'}"
    )


def configured_images(settings: Any) -> dict[str, str]:
    """Return every image the effective config names, keyed by where it is named."""
    from services.integration.repositories import load_repository_registry
    from services.integration.runtime_profiles import load_profiles, resolve_image
    from services.orchestration.checks.config import load_checks_configs
    from services.orchestration.gates.engine_source import IMAGE, load_gate_engine_configs

    images = {
        "GATE_ENGINE_BUILDER_IMAGE": settings.gate_engine_builder_image,
        "OPENHANDS_AGENT_SERVER_IMAGE": settings.openhands_agent_server_image,
        "SEMGREP_IMAGE": settings.semgrep_image,
        "TRIVY_IMAGE": settings.trivy_image,
    }
    images.update(
        {f"RUNTIME_IMAGES {name}": image for name, image in settings.runtime_images.items()}
    )
    for engine in load_gate_engine_configs(settings):
        if engine.enabled and engine.mode == IMAGE and engine.image:
            images[f"gate engine {engine.name}"] = engine.image
    for repo, config in load_checks_configs(settings).items():
        if config.image:
            images[f"checks of {repo}"] = resolve_image(config.image, settings)
    profiles = load_profiles(settings)
    for repository in load_repository_registry(settings).repositories.values():
        profile = profiles.get(repository.profile) if repository.profile else None
        if profile is None:
            continue
        if profile.sandbox_image:
            images[f"profile {profile.name} sandbox"] = resolve_image(
                profile.sandbox_image, settings
            )
        checks_image = str((profile.checks or {}).get("image") or "")
        if checks_image:
            images[f"profile {profile.name} checks"] = resolve_image(checks_image, settings)
    return {where: image for where, image in images.items() if image}


def image_problems(settings: Any) -> list[str]:
    """Return why the effective config names an image the policy does not admit.

    The tenant's own image_exceptions.yaml must parse and stay within the tenant's scope.
    """
    from services.integration.image_exceptions import read_tenant
    from services.orchestration.gates import registry_mirrors

    try:
        policy = load_policy(settings.environment)
        registries = registry_mirrors.current()
        read_tenant(settings, policy)
    except ConfigError as exc:
        return [str(exc)]
    mirrors = dict(registries.mirrors) if registries else {}
    problems = []
    for where, image in configured_images(settings).items():
        try:
            admit(image, policy, settings.tenant, mirrors)
        except ConfigError as exc:
            problems.append(f"{where}: {exc}")
    return problems
