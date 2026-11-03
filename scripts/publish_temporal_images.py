"""Build the Temporal server and admin-tools images and push them to ECR.

Each image is tagged `<upstream version>-<content hash>-<arch>`. The upstream version comes
from the digest the Dockerfile pins. The content hash covers the Dockerfile and every file it
copies, and the Dockerfile carries the upstream digest and the RDS CA bundle checksum, so the
tag changes whenever the image would. The repositories reject a pushed tag that already
exists, so an image whose tag is already present is reported and not rebuilt.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from services import process

REPO_ROOT = Path(__file__).resolve().parent.parent
BUILD_TIMEOUT_SECONDS = process.LONG_COMMAND_TIMEOUT_SECONDS
PLATFORMS = {"arm64": "linux/arm64", "amd64": "linux/amd64"}
_UPSTREAM_VERSION = re.compile(
    r"^ARG TEMPORAL_\w+_IMAGE=(?:[\w.-]+/)?temporalio/[\w-]+:([0-9.]+)@sha256:", re.M
)


@dataclass(frozen=True)
class ImageSpec:
    name: str
    dockerfile: str
    inputs: tuple[str, ...]


IMAGES = (
    ImageSpec("temporal-server", "docker/temporal.Dockerfile", ("docker/healthprobe",)),
    ImageSpec(
        "temporal-admin-tools",
        "docker/temporal-admin-tools.Dockerfile",
        ("docker/temporal-admin-tools.sh",),
    ),
)


def upstream_version(dockerfile: str) -> str:
    match = _UPSTREAM_VERSION.search(dockerfile)
    if match is None:
        raise ValueError("the Dockerfile pins no temporalio image by version and digest")
    return match.group(1)


def _input_files(spec: ImageSpec, root: Path) -> list[Path]:
    files = [root / spec.dockerfile]
    for entry in spec.inputs:
        path = root / entry
        files.extend(sorted(p for p in path.rglob("*") if p.is_file()) if path.is_dir() else [path])
    return files


def content_hash(spec: ImageSpec, root: Path) -> str:
    digest = hashlib.sha256()
    for path in _input_files(spec, root):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:12]


def image_tag(spec: ImageSpec, root: Path, arch: str) -> str:
    version = upstream_version((root / spec.dockerfile).read_text(encoding="utf-8"))
    return f"{version}-{content_hash(spec, root)}-{arch}"


def build_command(spec: ImageSpec, root: Path, image: str, arch: str) -> list[str]:
    return [
        "docker",
        "buildx",
        "build",
        "--platform",
        PLATFORMS[arch],
        "--provenance=false",
        "--sbom=false",
        "--file",
        str(root / spec.dockerfile),
        "--tag",
        image,
        "--push",
        str(root),
    ]


def _pushed_digest(ecr, repository: str, tag: str) -> str:
    try:
        response = ecr.describe_images(repositoryName=repository, imageIds=[{"imageTag": tag}])
    except ecr.exceptions.ImageNotFoundException:
        return ""
    return response["imageDetails"][0]["imageDigest"]


def _docker_login(ecr, registry: str) -> None:
    token = ecr.get_authorization_token()["authorizationData"][0]["authorizationToken"]
    password = base64.b64decode(token).decode().partition(":")[2]
    process.run(
        ["docker", "login", "--username", "AWS", "--password-stdin", registry],
        timeout=process.SHORT_COMMAND_TIMEOUT_SECONDS,
        input=password.encode(),
        check=True,
        capture_output=True,
    )


def publish(ecr, registry: str, name_prefix: str, arch: str, root: Path = REPO_ROOT) -> list[str]:
    """Push every image whose tag is not yet in its repository, returning each reference."""
    references: list[str] = []
    logged_in = False
    for spec in IMAGES:
        repository = f"{name_prefix}/{spec.name}"
        tag = image_tag(spec, root, arch)
        image = f"{registry}/{repository}:{tag}"
        digest = _pushed_digest(ecr, repository, tag)
        if digest:
            print(f"[publish-temporal-images] {image} is already pushed")
        else:
            if not logged_in:
                _docker_login(ecr, registry)
                logged_in = True
            print(f"[publish-temporal-images] building {image} for {PLATFORMS[arch]}")
            process.run(
                build_command(spec, root, image, arch), timeout=BUILD_TIMEOUT_SECONDS, check=True
            )
            digest = _pushed_digest(ecr, repository, tag)
        references.append(f"{image}@{digest}")
    return references


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name-prefix", default="agentic-platform", help="Repository prefix.")
    parser.add_argument("--arch", choices=sorted(PLATFORMS), default="arm64")
    parser.add_argument("--region", default="", help="Registry region. Defaults to AWS_REGION.")
    args = parser.parse_args(argv)

    from scripts.aws_clients import configure_aws_fips
    from scripts.config import get_settings

    settings = get_settings()
    configure_aws_fips(settings)

    import boto3

    region = args.region or settings.aws_region
    session = boto3.Session(region_name=region)
    account = session.client("sts").get_caller_identity()["Account"]
    registry = f"{account}.dkr.ecr.{region}.amazonaws.com"
    for reference in publish(session.client("ecr"), registry, args.name_prefix, args.arch):
        print(reference)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
