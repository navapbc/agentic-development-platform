"""Registry mirrors and package indexes for the gate, checks, and scanner pods.

DNS Firewall resolves no public container registry, so the images these pods pull, and the base
images a gate engine or a project build starts from, come through mirrors that
`config/<environment>/registries.yaml` names. Without the file the pods pull as written.

- `mode: aws` mirrors each upstream through an ECR pull-through cache prefix in the account. The
  pods pull with the ECR token the activity worker issues, and packages come from their public
  indexes, which DNS Firewall allows by name.
- `mode: external` mirrors images and packages through an existing Artifactory or a compatible
  service. The activity worker reads a read-only token from the variable `credential.token_env`
  names and hands it to the pod, which uses it for the pulls and the package installs.

A buildah `registries.conf` drop-in redirects each upstream prefix to its mirror, so an engine
Dockerfile or a repository's checks image is used unchanged.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from services.integration.credentials import (
    env_config_path,
    load_env_config,
    require_current_config,
)

AWS = "aws"
EXTERNAL = "external"
MODES = (AWS, EXTERNAL)
PACKAGE_ENV = {"pypi": "PIP_INDEX_URL", "npm": "NPM_CONFIG_REGISTRY", "go": "GOPROXY"}


class RegistryConfigError(ValueError):
    """registries.yaml names an unknown mode, a malformed mirror, or no credential."""


@dataclass(frozen=True)
class RegistryConfig:
    mode: str
    mirrors: dict[str, str] = field(default_factory=dict)
    packages: dict[str, str] = field(default_factory=dict)
    username: str = ""
    token_env: str = ""

    @property
    def mirror_hosts(self) -> list[str]:
        return sorted({location.split("/", 1)[0] for location in self.mirrors.values()})

    @property
    def package_hosts(self) -> list[str]:
        return sorted({urlsplit(url).hostname or "" for url in self.packages.values()} - {""})


def parse(raw: dict[str, Any]) -> RegistryConfig:
    mode = raw.get("mode")
    if mode not in MODES:
        raise RegistryConfigError(f"registries.yaml mode must be one of {', '.join(MODES)}")
    mirrors = {str(k): str(v).rstrip("/") for k, v in (raw.get("mirrors") or {}).items()}
    for upstream, location in mirrors.items():
        if "://" in location or "/" not in location:
            raise RegistryConfigError(
                f"the mirror of {upstream} must be host/path without a scheme, not {location}"
            )
    packages = {str(k): str(v) for k, v in (raw.get("packages") or {}).items()}
    unknown = sorted(set(packages) - set(PACKAGE_ENV))
    if unknown:
        raise RegistryConfigError(f"registries.yaml names unknown package types {unknown}")
    if mode == AWS and packages:
        raise RegistryConfigError("aws mode takes packages from their public indexes, not URLs")
    credential = raw.get("credential") or {}
    if mode == EXTERNAL and not (credential.get("username") and credential.get("token_env")):
        raise RegistryConfigError("external mode needs credential.username and token_env")
    return RegistryConfig(
        mode=mode,
        mirrors=mirrors,
        packages=packages,
        username=str(credential.get("username") or ""),
        token_env=str(credential.get("token_env") or ""),
    )


def current() -> RegistryConfig | None:
    """Return the environment's registry config, or None when it has none."""
    from scripts.config import get_settings

    environment = get_settings().environment
    if not env_config_path("registries", environment=environment).exists():
        return None
    require_current_config("registries", environment=environment)
    return parse(load_env_config("registries", environment=environment))


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
