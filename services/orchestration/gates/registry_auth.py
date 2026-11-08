"""Registry credentials for the images a gate, checks, or scanner pod pulls with buildah.

These pods carry no AWS identity, and buildah inside the pod pulls their images, so on EKS a pull
from a private ECR repository has no credential of its own. The activity worker, which signs with
its Pod Identity role, requests an ECR authorization token for each ECR registry the pod pulls
from and copies a containers auth file into the pod through `copy_in`, so the token never appears
in a command line or the API server audit log. The pod's script exports `REGISTRY_AUTH_FILE` for
the pull and deletes the file before any project or scanner code runs. An image outside ECR, such
as one from a local registry, installs nothing.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

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


def auth_document(registries: dict[str, tuple[str, str]], token: TokenSource) -> dict[str, Any]:
    return {"auths": {host: {"auth": token(*where)} for host, where in sorted(registries.items())}}


async def install(
    ops: Any, pod: str, namespace: str, images: Iterable[str], token: TokenSource | None = None
) -> bool:
    """Copy an auth file for the ECR registries among `images` into the pod, if there are any."""
    registries = ecr_registries(images)
    if not registries:
        return False
    document = await asyncio.to_thread(auth_document, registries, token or ecr_token)
    with tempfile.TemporaryDirectory(prefix="registry-auth-") as directory:
        path = Path(directory) / Path(AUTH_FILE).name
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle)
        await ops.copy_in(pod, namespace, directory, AUTH_DIR)
    return True


def before_pull(installed: bool) -> str:
    return f"export REGISTRY_AUTH_FILE={AUTH_FILE}\n" if installed else ""


def after_pull(installed: bool) -> str:
    return f"rm -rf {AUTH_DIR}\nunset REGISTRY_AUTH_FILE\n" if installed else ""
