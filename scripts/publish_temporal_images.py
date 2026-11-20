"""Build the Temporal server, admin-tools, and UI images from source, scan them, and push them.

Each image is tagged `<Temporal version>-<content hash>-<arch>`. The version is the release tag
the Dockerfile builds. The content hash covers the Dockerfile and every file it copies, and the
Dockerfile carries the source tags, the module pins, and the RDS CA bundle checksum, so the tag
changes whenever the image would. The repositories reject a pushed tag that already exists, so
an image whose tag is already present is reported and not rebuilt.

Every image builds on the hardened base and is scanned with the Amazon Inspector SBOM generator
and the Inspector Scan API, as `scripts.image_scan` describes, and against the CIS benchmark, as
`scripts.al2023_base` describes, before it is pushed. An image with an unaccepted finding at
`--block-severity` or above, or a CIS rule that neither passes nor is accepted, is not pushed, and
the run exits 1.
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
LOG = "[publish-temporal-images]"
_SOURCE_VERSION = re.compile(r"^ARG TEMPORAL(?:_UI)?_VERSION=v([0-9.]+)$", re.M)


@dataclass(frozen=True)
class ImageSpec:
    name: str
    dockerfile: str
    inputs: tuple[str, ...]
    target: str = ""


IMAGES = (
    ImageSpec(
        "temporal-server",
        "docker/temporal.Dockerfile",
        ("docker/healthprobe", "docker/temporal-server-start.sh"),
    ),
    ImageSpec(
        "temporal-admin-tools",
        "docker/temporal-admin-tools.Dockerfile",
        ("docker/temporal-admin-tools.sh",),
    ),
    ImageSpec("temporal-ui", "docker/temporal-ui.Dockerfile", ("docker/temporal-ui-start.sh",)),
)


def source_version(dockerfile: str) -> str:
    """Return the Temporal release, without its leading v, that the Dockerfile builds."""
    match = _SOURCE_VERSION.search(dockerfile)
    if match is None:
        raise ValueError("the Dockerfile pins no TEMPORAL_VERSION or TEMPORAL_UI_VERSION release")
    return match.group(1)


# A Dockerfile that starts FROM ${AL2023_IMAGE} builds on the hardened base, so the base's inputs
# are its inputs too, and a change to the base retags it.
HARDENED_BASE_FILES = (
    "docker/al2023/base.Dockerfile",
    "docker/al2023/cis-scan.sh",
    "docker/al2023/cis-accepted.txt",
    "docker/al2023/cis-harden.sh",
)


def base_root(context: Path) -> Path:
    """Return the build context, or the context an engine directory sits in, that holds the base."""
    for candidate in (context, *context.parents):
        if (candidate / HARDENED_BASE_FILES[0]).is_file():
            return candidate
    raise FileNotFoundError(f"no {HARDENED_BASE_FILES[0]} in {context} or above it")


def _input_files(spec: ImageSpec, root: Path) -> list[tuple[str, Path]]:
    dockerfile = root / spec.dockerfile
    files = [(spec.dockerfile, dockerfile)]
    if spec.dockerfile not in HARDENED_BASE_FILES and "${AL2023_IMAGE}" in dockerfile.read_text(
        encoding="utf-8"
    ):
        repository = base_root(root)
        files.extend((name, repository / name) for name in HARDENED_BASE_FILES)
    for entry in spec.inputs:
        path = root / entry
        paths = sorted(p for p in path.rglob("*") if p.is_file()) if path.is_dir() else [path]
        files.extend((p.relative_to(root).as_posix(), p) for p in paths)
    return files


def content_hash(spec: ImageSpec, root: Path) -> str:
    digest = hashlib.sha256()
    for name, path in _input_files(spec, root):
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:12]


def image_tag(spec: ImageSpec, root: Path, arch: str) -> str:
    version = source_version((root / spec.dockerfile).read_text(encoding="utf-8"))
    return f"{version}-{content_hash(spec, root)}-{arch}"


def build_command(
    spec: ImageSpec,
    root: Path,
    image: str,
    arch: str,
    build_args: dict[str, str] | None = None,
    *,
    output: str = "--push",
) -> list[str]:
    arguments = [
        part
        for name, value in (build_args or {}).items()
        for part in ("--build-arg", f"{name}={value}")
    ]
    return [
        "docker",
        "buildx",
        "build",
        "--platform",
        PLATFORMS[arch],
        "--provenance=false",
        "--sbom=false",
        *arguments,
        "--file",
        str(root / spec.dockerfile),
        *(["--target", spec.target] if spec.target else []),
        "--tag",
        image,
        output,
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


def publish(publisher, root: Path = REPO_ROOT) -> list[str]:
    """Push every image whose tag is not yet in its repository, returning each reference."""
    from scripts.publish_platform_images import build_context

    with build_context(root) as context:
        return [
            publisher.built(
                spec, context, source_version((context / spec.dockerfile).read_text("utf-8"))
            )
            for spec in IMAGES
        ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name-prefix", default="agentic-platform", help="Repository prefix.")
    parser.add_argument("--arch", choices=sorted(PLATFORMS), default="arm64")
    parser.add_argument("--region", default="", help="Registry region. Defaults to AWS_REGION.")
    parser.add_argument(
        "--block-severity",
        default="high",
        choices=("low", "medium", "high", "critical"),
        help="Lowest Inspector severity that keeps an image from being pushed.",
    )
    parser.add_argument(
        "--exceptions",
        default="",
        help="A local image_exceptions.yaml to read in place of the platform exceptions file.",
    )
    parser.add_argument(
        "--cis-evidence",
        type=Path,
        default=Path("cis-evidence"),
        help="Directory for the CIS benchmark scan results of the base and of each image.",
    )
    args = parser.parse_args(argv)

    from scripts.aws_clients import configure_aws_fips
    from scripts.config import get_settings

    settings = get_settings()
    configure_aws_fips(settings)

    import boto3
    from services.integration.image_exceptions import platform_exceptions

    from scripts.image_scan import ImageScanner
    from scripts.publish_platform_images import Publisher

    region = args.region or settings.aws_region
    session = boto3.Session(region_name=region)
    account = session.client("sts").get_caller_identity()["Account"]
    registry = f"{account}.dkr.ecr.{region}.amazonaws.com"
    scanner = ImageScanner(
        session,
        args.arch,
        block=args.block_severity,
        exceptions=platform_exceptions(settings, args.exceptions),
    )
    from scripts.al2023_base import Base

    publisher = Publisher(
        session.client("ecr"),
        registry,
        args.name_prefix,
        args.arch,
        scanner,
        log=LOG,
        base=Base(args.arch, evidence=args.cis_evidence),
    )
    references = publish(publisher)
    if publisher.refused:
        for refusal in publisher.refused:
            print(f"{LOG} not pushed: {refusal}")
        print(
            f"{LOG} {len(publisher.refused)} image(s) carry blocking findings and were not "
            "pushed. Raise the vulnerable modules in docker/al2023/versions.env, or accept each "
            "finding in the exceptions file with a reason and an expiry."
        )
        return 1
    for reference in references:
        print(reference)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
