"""Registry mirrors and credentials for the images a gate, checks, or scanner pod pulls.

These pods carry no AWS identity that can pull, and buildah inside the pod pulls their images, so
on EKS a pull from a private ECR repository has no credential of its own. The activity worker,
which signs with its Pod Identity role, requests an ECR authorization token for each ECR registry
the pod pulls from and copies a containers auth file into the pod through `copy_in`, so the token
never appears in a command line or the API server audit log. The pod's script exports
`REGISTRY_AUTH_FILE` for the pull and deletes the file before any project or scanner code runs. An
image outside ECR, such as one from a local registry, installs nothing. With a registries.yaml, the
pod also receives the buildah mirror drop-in, and in external mode the mirror's token and the
package credential.

Every image is admitted first, as `image_admission` describes. The pod receives a policy.json that
rejects every registry the image policy does not admit, and with signature verification on, the
notation trust policy its pull verifies each signed image against.
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from services.orchestration.gates import image_admission, registry_mirrors

AUTH_DIR = "/run/registry-auth"
AUTH_FILE = f"{AUTH_DIR}/auth.json"

_ECR_HOST = re.compile(
    r"^(?P<account>\d{12})\.dkr\.ecr(?:-fips)?\.(?P<region>[a-z0-9-]+)\.amazonaws\.com(?:\.cn)?$"
)

TokenSource = Callable[[str, str], str]


def ecr_registries(images: Iterable[str]) -> dict[str, tuple[str, str]]:
    """Map each ECR registry host among `images` to its account and region."""
    registries: dict[str, tuple[str, str]] = {}
    for image in images:
        host = image.split("/", 1)[0] if "/" in image else ""
        match = _ECR_HOST.match(host)
        if match:
            registries[host] = (match["account"], match["region"])
    return registries


def ecr_token(account: str, region: str) -> str:
    """Return the base64 `AWS:<password>` token ECR issues for `account`, signed by the worker."""
    import boto3

    ecr = boto3.client("ecr", region_name=region)
    data = ecr.get_authorization_token(registryIds=[account])["authorizationData"]
    return data[0]["authorizationToken"]


PACKAGE_AUTH_DIR = "/run/package-auth"
MIRRORS_DIR = "/etc/containers/registries.conf.d"
MIRRORS_FILE = "50-agentic-dev-platform-mirrors.conf"


@dataclass(frozen=True)
class RegistrySetup:
    """What `install` placed in a pod, and the script fragments that use it.

    The registry credential lives only for the pull. The package credential stays for the run,
    because the project's own command installs its packages, so it is read-only by contract.
    """

    authenticated: bool = False
    package_auth: bool = False
    packages: Mapping[str, str] = field(default_factory=dict)
    verify: str = ""

    def before_pull(self) -> str:
        auth = f"export REGISTRY_AUTH_FILE={AUTH_FILE} DOCKER_CONFIG={AUTH_DIR}\n"
        return (auth if self.authenticated else "") + self.verify

    def after_pull(self) -> str:
        return (
            f"rm -rf {AUTH_DIR}\nunset REGISTRY_AUTH_FILE DOCKER_CONFIG\n"
            if self.authenticated
            else ""
        )

    def _environment(self) -> dict[str, str]:
        environment = dict(self.packages)
        if self.package_auth:
            environment["NETRC"] = f"{PACKAGE_AUTH_DIR}/netrc"
            environment["NPM_CONFIG_USERCONFIG"] = f"{PACKAGE_AUTH_DIR}/npmrc"
        return environment

    def run_flags(self) -> str:
        """Flags that give a `buildah run` container the package indexes and their credential."""
        flags = [f"-e {shlex.quote(f'{k}={v}')}" for k, v in sorted(self._environment().items())]
        if self.package_auth:
            flags.insert(0, f"-v {PACKAGE_AUTH_DIR}:{PACKAGE_AUTH_DIR}:ro")
        return "".join(f"{flag} " for flag in flags)

    def build_flags(self) -> str:
        """Flags that give the RUN steps of a `buildah bud` the package indexes and credential."""
        flags = [f"--env {shlex.quote(f'{k}={v}')}" for k, v in sorted(self._environment().items())]
        if self.package_auth:
            flags.insert(0, f"--volume {PACKAGE_AUTH_DIR}:{PACKAGE_AUTH_DIR}:ro")
        return "".join(f"{flag} " for flag in flags)


NO_REGISTRY = RegistrySetup()


async def _copy_files(ops: Any, pod: str, namespace: str, files: Mapping[str, str], target: str):
    with tempfile.TemporaryDirectory(prefix="registry-") as directory:
        for name, content in files.items():
            descriptor = os.open(
                Path(directory) / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
        await ops.copy_in(pod, namespace, directory, target)


def _external_token(config: registry_mirrors.RegistryConfig | None) -> str | None:
    """Return the external registry's token, or None for a config that authenticates with ECR."""
    if config is None or config.mode != registry_mirrors.EXTERNAL:
        return None
    token = os.environ.get(config.token_env)
    if not token:
        raise RegistryAuthError(f"{config.token_env} holds no registry token")
    return token


async def install(
    ops: Any,
    pod: str,
    namespace: str,
    images: Iterable[str],
    token: TokenSource | None = None,
    registries: registry_mirrors.RegistryConfig | None | bool = True,
) -> RegistrySetup:
    """Place the mirror config and the credentials the pod's pulls and installs need.

    `registries` defaults to the environment's registries.yaml. The ECR token covers each ECR
    registry among `images` and the mirrors, and an external mirror's token comes from the
    variable its config names.
    """
    images = [image for image in images if image]
    admission = await image_admission.admit_images(images)
    policies = {image_admission.POLICY_FILE: image_admission.containers_policy(admission)}
    await _copy_files(ops, pod, namespace, policies, image_admission.POLICY_DIR)
    verify = image_admission.verify_script(admission)
    if verify:
        trust = {image_admission.TRUST_POLICY_FILE: image_admission.trust_policy(admission)}
        await _copy_files(ops, pod, namespace, trust, image_admission.NOTATION_CONFIG_DIR)
    config = registry_mirrors.current() if registries is True else registries or None
    mirror_images = [f"{location}/x" for location in config.mirrors.values()] if config else []
    ecr = ecr_registries([*images, *mirror_images])
    entries = await asyncio.to_thread(
        lambda: {host: (token or ecr_token)(*where) for host, where in sorted(ecr.items())}
    )
    external_token = _external_token(config)
    if external_token is not None:
        entries.update(registry_mirrors.basic_auth(config, external_token))
    if config is not None and config.mirrors:
        conf = {MIRRORS_FILE: registry_mirrors.registries_conf(config)}
        await _copy_files(ops, pod, namespace, conf, MIRRORS_DIR)
    if entries:
        document = registry_mirrors.auth_document(entries)
        # notation reads the same credentials from the Docker config DOCKER_CONFIG names.
        files = {Path(AUTH_FILE).name: document, "config.json": document}
        await _copy_files(ops, pod, namespace, files, AUTH_DIR)
    package_auth = bool(external_token is not None and config.packages)
    if package_auth:
        files = registry_mirrors.package_auth_files(config, external_token)
        await _copy_files(ops, pod, namespace, files, PACKAGE_AUTH_DIR)
    return RegistrySetup(
        authenticated=bool(entries),
        package_auth=package_auth,
        packages=registry_mirrors.package_env(config) if config else {},
        verify=verify,
    )


class RegistryAuthError(RuntimeError):
    """The registry credential the environment names is not available to the worker."""
