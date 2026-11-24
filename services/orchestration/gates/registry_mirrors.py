"""Registry mirrors and package indexes for the gate, checks, and scanner pods.

DNS Firewall resolves no public container registry, so the images these pods pull, and the base
images a gate engine or a project build starts from, come through mirrors that the platform's
registries.yaml names. Without the platform file the pods pull as written.

The platform file sets `mode`, `mirrors`, and `packages`, which follow the deployment's caches and
DNS Firewall. A tenant's registries.yaml adds or replaces `mirrors` and `packages` entries only on
a host the platform file names, and names the tenant's own `credential`, so each tenant presents
its own registry identity. A tenant file may not set `mode`.

- `mode: aws` mirrors each upstream through an ECR pull-through cache prefix in the account. The
  pods pull with the ECR token the activity worker issues, and packages come from their public
  indexes, which DNS Firewall allows by name.
- `mode: external` mirrors images and packages through an existing Artifactory or a compatible
  service. The activity worker reads the tenant's read-only token from `REGISTRY_TOKEN`, which
  only the tenant's own settings or registry-token secret supply, and hands it to the pod, which
  uses it for the pulls and the package installs.

A buildah `registries.conf` drop-in redirects each upstream prefix to its mirror, so an engine
Dockerfile or a repository's checks image is used unchanged.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import yaml

from services.integration.credentials import (
    ConfigError,
    config_version,
    current_config_version,
    env_config_dir,
)

AWS = "aws"
EXTERNAL = "external"
MODES = (AWS, EXTERNAL)
CONFIG = "registries"
PACKAGE_ENV = {"pypi": "PIP_INDEX_URL", "npm": "NPM_CONFIG_REGISTRY", "go": "GOPROXY"}
TOKEN_ENV = "REGISTRY_TOKEN"

_REGISTRY_PATH = re.compile(r"^[A-Za-z0-9.-]+(:[0-9]+)?(/[A-Za-z0-9._/-]*)?$")
_PACKAGE_URL_REFUSED = re.compile(r"[\s,|\"'\\]")


class RegistryConfigError(ValueError):
    """registries.yaml names an unknown mode, a malformed mirror, or no credential."""


@dataclass(frozen=True)
class RegistryConfig:
    mode: str
    mirrors: dict[str, str] = field(default_factory=dict)
    packages: dict[str, str] = field(default_factory=dict)
    username: str = ""

    @property
    def mirror_hosts(self) -> list[str]:
        return sorted({location.split("/", 1)[0] for location in self.mirrors.values()})

    @property
    def package_hosts(self) -> list[str]:
        return sorted({urlsplit(url).hostname or "" for url in self.packages.values()} - {""})


def _mirrors(raw: dict[str, Any]) -> dict[str, str]:
    mirrors = {str(k): str(v).rstrip("/") for k, v in (raw.get("mirrors") or {}).items()}
    for upstream, location in mirrors.items():
        if not _REGISTRY_PATH.match(upstream):
            raise RegistryConfigError(f"the mirror upstream {upstream!r} is not a registry host")
        if "/" not in location or not _REGISTRY_PATH.match(location):
            raise RegistryConfigError(
                f"the mirror of {upstream} must be host/path without a scheme, not {location!r}"
            )
    return mirrors


def _packages(raw: dict[str, Any]) -> dict[str, str]:
    packages = {str(k): str(v) for k, v in (raw.get("packages") or {}).items()}
    unknown = sorted(set(packages) - set(PACKAGE_ENV))
    if unknown:
        raise RegistryConfigError(f"registries.yaml names unknown package types {unknown}")
    for kind, url in packages.items():
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or _PACKAGE_URL_REFUSED.search(url):
            raise RegistryConfigError(
                f"the {kind} package index must be one https URL, not {url!r}"
            )
    return packages


def _username(value: object) -> str:
    username = str(value or "").strip()
    if "/" in username or any(character.isspace() for character in username):
        raise RegistryConfigError(f"credential.username {value!r} is not a username")
    return username


def tenant_mirrors(tenant: dict[str, Any]) -> dict[str, str]:
    """Return the mirrors a tenant's registries.yaml adds, refusing a tenant file that sets mode."""
    if "mode" in tenant:
        raise RegistryConfigError("a tenant registries.yaml may not set mode")
    return _mirrors(tenant)


def parse(platform: dict[str, Any], tenant: dict[str, Any] | None = None) -> RegistryConfig:
    """Merge the tenant's registries.yaml over the platform's, within the platform's hosts."""
    mode = platform.get("mode")
    if mode not in MODES:
        raise RegistryConfigError(f"registries.yaml mode must be one of {', '.join(MODES)}")
    if "credential" in platform:
        raise RegistryConfigError(
            "the platform registries.yaml sets credential, which each tenant names in its own "
            "registries.yaml"
        )
    base = RegistryConfig(mode=mode, mirrors=_mirrors(platform), packages=_packages(platform))
    tenant = tenant or {}
    mirrors = {**base.mirrors, **tenant_mirrors(tenant)}
    packages = {**base.packages, **_packages(tenant)}
    if mode == AWS and packages:
        raise RegistryConfigError("aws mode takes packages from their public indexes, not URLs")
    merged = RegistryConfig(mode=mode, mirrors=mirrors, packages=packages)
    for kind, hosts, allowed in (
        ("mirror", merged.mirror_hosts, base.mirror_hosts),
        ("package", merged.package_hosts, base.package_hosts),
    ):
        outside = sorted(set(hosts) - set(allowed))
        if outside:
            raise RegistryConfigError(
                f"the tenant registries.yaml names the {kind} host {', '.join(outside)}, which the "
                f"platform registries.yaml does not name. A tenant {kind} must be on "
                f"{', '.join(allowed) or 'a platform host'}"
            )
    credential = tenant.get("credential") or {}
    if "token_env" in credential:
        raise RegistryConfigError(
            f"the tenant registries.yaml sets credential.token_env. The registry token is always "
            f"{TOKEN_ENV} in the tenant's own settings, so remove token_env"
        )
    username = _username(credential.get("username"))
    if mode == EXTERNAL and not username:
        raise RegistryConfigError(
            "external mode needs credential.username in the tenant's registries.yaml"
        )
    return RegistryConfig(mode=mode, mirrors=mirrors, packages=packages, username=username)


def parse_texts(platform: str, tenant: str | None) -> RegistryConfig:
    """Parse the platform's and the tenant's registries.yaml texts, refusing an old version."""
    expected = current_config_version(CONFIG)
    raw: dict[str, dict[str, Any]] = {}
    for owner, text in (("platform", platform), ("tenant", tenant or "")):
        loaded = yaml.safe_load(text) or {}
        if not isinstance(loaded, dict):
            raise RegistryConfigError(f"the {owner} {CONFIG}.yaml must be a mapping")
        if loaded and config_version(loaded) < expected:
            raise ConfigError(
                f"the {owner} {CONFIG}.yaml is version {config_version(loaded)} but this release "
                f"expects version {expected}. Run `inv config-check` and add the new keys."
            )
        raw[owner] = loaded
    return parse(raw["platform"], raw["tenant"])


def current() -> RegistryConfig | None:
    """Return the registry config of this context's tenant, or None without a platform file."""
    from services.integration.tenant_config import tenant_file

    platform = env_config_dir() / f"{CONFIG}.yaml"
    if not platform.is_file():
        return None
    tenant = tenant_file(CONFIG)
    return parse_texts(
        platform.read_text(encoding="utf-8"),
        tenant.read_text(encoding="utf-8") if tenant is not None else None,
    )


def registries_conf(config: RegistryConfig) -> str:
    """Return the buildah registries.conf drop-in that sends each upstream to its mirror."""
    blocks = []
    for upstream, location in sorted(config.mirrors.items()):
        blocks.append(
            f'[[registry]]\nprefix = "{upstream}"\nlocation = "{upstream}"\n\n'
            f'[[registry.mirror]]\nlocation = "{location}"\n'
        )
    return "\n".join(blocks)


def basic_auth(config: RegistryConfig, token: str) -> dict[str, str]:
    """Return the auth.json entry of each mirror host for a token-authenticated registry."""
    encoded = base64.b64encode(f"{config.username}:{token}".encode()).decode()
    return {host: encoded for host in config.mirror_hosts}


def package_env(config: RegistryConfig) -> dict[str, str]:
    """Return the package index settings, which carry no credential."""
    return {PACKAGE_ENV[kind]: url for kind, url in sorted(config.packages.items())}


def package_auth_files(config: RegistryConfig, token: str) -> dict[str, str]:
    """Return the netrc and npmrc that authenticate the package installs with `token`.

    pip and the Go command read the netrc NETRC names, and npm reads the npmrc
    NPM_CONFIG_USERCONFIG names, so the token is a file in the pod and never a command argument.
    """
    netrc = "".join(
        f"machine {host}\nlogin {config.username}\npassword {token}\n"
        for host in config.package_hosts
    )
    npm = config.packages.get("npm", "")
    npmrc = ""
    if npm:
        parsed = urlsplit(npm)
        npmrc = f"//{parsed.netloc}{parsed.path.rstrip('/')}/:_authToken={token}\n"
    files = {"netrc": netrc}
    if npmrc:
        files["npmrc"] = npmrc
    return files


def auth_document(entries: dict[str, str]) -> str:
    return json.dumps({"auths": {host: {"auth": auth} for host, auth in sorted(entries.items())}})
