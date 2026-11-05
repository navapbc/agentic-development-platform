"""Build the platform container images, mirror the upstream ones, and push them to ECR.

The ECS tasks pull only from ECR, because DNS Firewall resolves no public registry.

- The platform image carries the Temporal Worker Deployment build ID, a release version and a
  UTC timestamp that `scripts.build_id` orders. Its tag is `<build id>-<source hash>-<arch>`,
  so a build whose sources are already in the repository reuses that image and its build ID
  instead of minting a new Worker Deployment version.
- The repository-intelligence and collector images are tagged `<version>-<content hash>-<arch>`.
- Langfuse web, Langfuse worker, and ClickHouse are copied by the digest `compose.yaml` pins
  and tagged with their upstream version, and the copy must keep that digest.

The images are built from the files git tracks or would track, so ignored local artifacts such
as the binaries under `vendor/bin` never reach an image.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import yaml
from services import process

from scripts.build_id import generate_build_id, release_version, source_digest
from scripts.publish_temporal_images import (
    BUILD_TIMEOUT_SECONDS,
    PLATFORMS,
    ImageSpec,
    _docker_login,
    _pushed_digest,
    build_command,
    content_hash,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG = "[publish-platform-images]"
PLATFORM = ImageSpec("platform", "docker/Dockerfile", ())
REPO_INTELLIGENCE = ImageSpec(
    "repo-intelligence",
    "docker/repo-intelligence.Dockerfile",
    (
        "pyproject.toml",
        "uv.lock",
        "README.md",
        "LICENSE",
        "NOTICE",
        "services",
        "adapters",
        "schemas",
        "database",
        "scripts",
    ),
)
COLLECTOR = ImageSpec("otel-collector", "docker/otel-collector.Dockerfile", ("docker/healthprobe",))
MIRRORED_SERVICES = {
    "langfuse-web": "langfuse-web",
    "langfuse-worker": "langfuse-worker",
    "clickhouse": "clickhouse",
}
_COLLECTOR_VERSION = re.compile(r"^FROM \S*opentelemetry-collector-contrib:([0-9.]+)@sha256:", re.M)
_PINNED_REFERENCE = re.compile(
    r"^(?P<source>[^@\s]+):(?P<tag>[\w.-]+)@(?P<digest>sha256:[0-9a-f]{64})$"
)


class PublishError(Exception):
    """An image could not be published as pinned."""


@dataclass(frozen=True)
class Mirror:
    name: str
    source: str
    tag: str
    digest: str


@dataclass(frozen=True)
class AcliBuild:
    install: bool = False
    version: str = ""
    sha256: str = ""

    def build_args(self) -> dict[str, str]:
        if not self.install:
            return {"INSTALL_ACLI": "false"}
        return {"INSTALL_ACLI": "true", "ACLI_VERSION": self.version, "ACLI_SHA256": self.sha256}


def mirrors(compose: str) -> list[Mirror]:
    """Read the pinned upstream image of each mirrored service from `compose.yaml`."""
    services = yaml.safe_load(compose)["services"]
    found: list[Mirror] = []
    for name, service in MIRRORED_SERVICES.items():
        reference = services[service]["image"]
        match = _PINNED_REFERENCE.match(reference)
        if match is None:
            raise PublishError(
                f"compose.yaml pins {service} as {reference}, without a tag and digest"
            )
        found.append(Mirror(name, match["source"], match["tag"], match["digest"]))
    return found


def collector_version(dockerfile: str) -> str:
    match = _COLLECTOR_VERSION.search(dockerfile)
    if match is None:
        raise PublishError("the collector Dockerfile pins no collector image by version and digest")
    return match.group(1)


def platform_source_hash(root: Path, acli: AcliBuild, arch: str) -> str:
    digest = hashlib.sha256(source_digest(root).encode())
    for name, value in sorted(acli.build_args().items()):
        digest.update(f"\0{name}={value}".encode())
    digest.update(f"\0{arch}".encode())
    return digest.hexdigest()[:12]


def _tagged_images(ecr, repository: str) -> Iterator[tuple[str, str]]:
    paginator = ecr.get_paginator("describe_images")
    for page in paginator.paginate(repositoryName=repository, filter={"tagStatus": "TAGGED"}):
        for detail in page["imageDetails"]:
            for tag in detail.get("imageTags", []):
                yield tag, detail["imageDigest"]


def published_platform_tag(ecr, repository: str, source_hash: str, arch: str) -> tuple[str, str]:
    """Return the tag and digest of a platform image already built from these sources."""
    suffix = f"-{source_hash}-{arch}"
    for tag, digest in _tagged_images(ecr, repository):
        if tag.endswith(suffix):
            return tag, digest
    return "", ""


def build_id_of(tag: str) -> str:
    return tag.rsplit("-", 2)[0]


@contextmanager
def build_context(root: Path) -> Iterator[Path]:
    """Copy the files git tracks or would track into a temporary build context."""
    listing = process.run(
        ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        timeout=process.SHORT_COMMAND_TIMEOUT_SECONDS,
        capture_output=True,
        check=True,
    )
    with tempfile.TemporaryDirectory(prefix="platform-image-") as directory:
        context = Path(directory)
        for name in filter(None, listing.stdout.decode().split("\0")):
            source = root / name
            if not source.is_file():
                continue
            target = context / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        yield context


class Publisher:
    def __init__(self, ecr, registry: str, name_prefix: str, arch: str) -> None:
        self.ecr = ecr
        self.registry = registry
        self.name_prefix = name_prefix
        self.arch = arch
        self._logged_in = False

    def _repository(self, name: str) -> str:
        return f"{self.name_prefix}/{name}"

    def _login(self) -> None:
        if not self._logged_in:
            _docker_login(self.ecr, self.registry)
            self._logged_in = True

    def _build(
        self, spec: ImageSpec, context: Path, tag: str, build_args: dict[str, str] | None = None
    ) -> str:
        repository = self._repository(spec.name)
        image = f"{self.registry}/{repository}:{tag}"
        digest = _pushed_digest(self.ecr, repository, tag)
        if digest:
            print(f"{LOG} {image} is already pushed")
        else:
            self._login()
            print(f"{LOG} building {image} for {PLATFORMS[self.arch]}")
            process.run(
                build_command(spec, context, image, self.arch, build_args),
                timeout=BUILD_TIMEOUT_SECONDS,
                check=True,
            )
            digest = _pushed_digest(self.ecr, repository, tag)
        return f"{image}@{digest}"

    def platform(self, context: Path, acli: AcliBuild) -> str:
        repository = self._repository(PLATFORM.name)
        source_hash = platform_source_hash(context, acli, self.arch)
        tag, digest = published_platform_tag(self.ecr, repository, source_hash, self.arch)
        if tag:
            image = f"{self.registry}/{repository}:{tag}"
            print(f"{LOG} {image} is already pushed, build ID {build_id_of(tag)}")
            return f"{image}@{digest}"
        build_id = generate_build_id()
        build_args = {"AGENT_BUILD_ID": build_id, **acli.build_args()}
        return self._build(PLATFORM, context, f"{build_id}-{source_hash}-{self.arch}", build_args)

    def built(self, spec: ImageSpec, context: Path, version: str) -> str:
        return self._build(spec, context, f"{version}-{content_hash(spec, context)}-{self.arch}")

    def mirror(self, mirror: Mirror) -> str:
        repository = self._repository(mirror.name)
        image = f"{self.registry}/{repository}:{mirror.tag}"
        digest = _pushed_digest(self.ecr, repository, mirror.tag)
        if digest:
            print(f"{LOG} {image} is already pushed")
        else:
            self._login()
            print(f"{LOG} copying {mirror.source}@{mirror.digest} to {image}")
            process.run(
                [
                    "docker",
                    "buildx",
                    "imagetools",
                    "create",
                    "--tag",
                    image,
                    f"{mirror.source}@{mirror.digest}",
                ],
                timeout=BUILD_TIMEOUT_SECONDS,
                check=True,
            )
            digest = _pushed_digest(self.ecr, repository, mirror.tag)
        if digest != mirror.digest:
            raise PublishError(
                f"{image} has digest {digest}, and compose.yaml pins {mirror.digest}. "
                "Delete the tag from the repository and publish again."
            )
        return f"{image}@{digest}"


def publish(publisher: Publisher, acli: AcliBuild, root: Path = REPO_ROOT) -> list[str]:
    """Push every image whose tag is not yet in its repository, returning each reference."""
    with build_context(root) as context:
        references = [
            publisher.platform(context, acli),
            publisher.built(REPO_INTELLIGENCE, context, release_version()),
            publisher.built(
                COLLECTOR,
                context,
                collector_version((context / COLLECTOR.dockerfile).read_text(encoding="utf-8")),
            ),
        ]
    compose = (root / "compose.yaml").read_text(encoding="utf-8")
    references += [publisher.mirror(mirror) for mirror in mirrors(compose)]
    return references


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name-prefix", default="agentic-platform", help="Repository prefix.")
    parser.add_argument("--arch", choices=sorted(PLATFORMS), default="arm64")
    parser.add_argument("--region", default="", help="Registry region. Defaults to AWS_REGION.")
    parser.add_argument("--install-acli", action="store_true", help="Install acli in the image.")
    parser.add_argument("--acli-version", default="")
    parser.add_argument("--acli-sha256", default="")
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
    publisher = Publisher(session.client("ecr"), registry, args.name_prefix, args.arch)
    acli = AcliBuild(args.install_acli, args.acli_version, args.acli_sha256)
    try:
        references = publish(publisher, acli)
    except PublishError as exc:
        print(f"{LOG} {exc}")
        return 1
    for reference in references:
        print(reference)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
