"""Build the platform container images, mirror the upstream ones, and push them to ECR.

The ECS tasks and the EKS pods pull only from ECR, because DNS Firewall resolves no public
registry.

- The platform image carries the Temporal Worker Deployment build ID, a release version and a
  UTC timestamp that `scripts.build_id` orders. Its tag is `<build id>-<source hash>-<arch>`,
  so a build whose sources are already in the repository reuses that image and its build ID
  instead of minting a new Worker Deployment version.
- The repository-intelligence, collector, TLS sidecar, OPA, eks-deployer, and buildah builder
  images, the example gate engine images under `examples/gate-engines`, and the catalog sandbox
  images `agent-server-jvm` and `agent-server-go` are tagged `<version>-<content hash>-<arch>`.
- Langfuse web, Langfuse worker, and ClickHouse are copied by the digest `compose.yaml` pins,
  LiteLLM by the digest its cluster manifest pins, and the OpenHands agent-server, semgrep, and
  trivy images by the digests their settings default to. Each copy is tagged with its upstream
  version and must keep the pinned digest.

The images are built from the files git tracks or would track, so ignored local artifacts such
as the binaries under `vendor/bin` never reach an image.

The references are written to `images.auto.tfvars.json` in the `private-bedrock` composition
root, which Terraform loads with no flag, and optionally to a manifest that marks the images
this run pushed.

Every image is scanned with the Amazon Inspector SBOM generator and the Inspector Scan API before
it is pushed, as `scripts.image_scan` describes. An image with an unaccepted finding at
`--block-severity` or above is not pushed, and the run writes no references.

With a signing profile, every image a tenant pod may run from the platform catalog is signed with
it through notation and the AWS Signer plugin, run from the buildah builder image this publish
built, so the publishing host needs neither installed. An image that already carries a signature
is not signed again.
"""

from __future__ import annotations

import argparse
import hashlib
import json
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
IMAGES_FILE = (
    REPO_ROOT / "infra" / "terraform" / "examples" / "private-bedrock" / "images.auto.tfvars.json"
)
TFVARS_KEYS = {
    "platform": "container_platform_image",
    "repo-intelligence": "container_repo_intelligence_image",
    "otel-collector": "container_collector_image",
    "tls-sidecar": "container_tls_sidecar_image",
    "langfuse-web": "container_langfuse_web_image",
    "langfuse-worker": "container_langfuse_worker_image",
    "clickhouse": "container_clickhouse_image",
    "opa": "eks_opa_image",
    "litellm": "eks_litellm_image",
    "openhands-agent-server": "eks_agent_server_image",
    "buildah": "eks_buildah_image",
    "semgrep": "eks_semgrep_image",
    "trivy": "eks_trivy_image",
    "eks-deployer": "eks_deployer_image",
}
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
        "docker/platform-entrypoint.sh",
    ),
)
COLLECTOR = ImageSpec("otel-collector", "docker/otel-collector.Dockerfile", ("docker/healthprobe",))
OPA = ImageSpec("opa", "docker/opa.Dockerfile", ("docker/healthprobe",))
EKS_DEPLOYER = ImageSpec(
    "eks-deployer",
    "docker/eks-deployer.Dockerfile",
    (
        "LICENSE",
        "NOTICE",
        "scripts/__init__.py",
        "scripts/eks_render.py",
        "scripts/tenant_render.py",
        "scripts/eks_deploy_task.py",
        "services/__init__.py",
        "services/process.py",
        "deploy/kubernetes",
        "openhands/policies",
        "config/platform/litellm-config.yaml",
        "config/platform/litellm_redaction.py",
        "config/policies",
    ),
)
BUILDAH = ImageSpec("buildah", "docker/buildah.Dockerfile", ())
TLS_SIDECAR = ImageSpec(
    "tls-sidecar", "docker/tls-sidecar.Dockerfile", ("docker/tls-sidecar", "docker/healthprobe")
)
GATE_ENGINE_ROOT = "examples/gate-engines"
GATE_ENGINE_IMAGES_KEY = "eks_gate_engine_images"
GATE_ENGINES = {
    name: ImageSpec(f"gate-engine-{name}", "Dockerfile", ("bin", "policy.py", "rules.toml"))
    for name in ("security-policy", "test-policy")
}
RUNTIME_IMAGES_KEY = "eks_runtime_images"
RUNTIME_IMAGES = {
    name: ImageSpec(name, f"docker/{name}.Dockerfile", ())
    for name in ("agent-server-jvm", "agent-server-go")
}
MIRRORED_SERVICES = {
    "langfuse-web": "langfuse-web",
    "langfuse-worker": "langfuse-worker",
    "clickhouse": "clickhouse",
}
MIRRORED_SETTINGS = {
    "openhands-agent-server": "openhands_agent_server_image",
    "semgrep": "semgrep_image",
    "trivy": "trivy_image",
}
# The catalog repositories a tenant pod may run, which the platform signs.
SIGNED_CATALOG = frozenset(
    {
        "semgrep",
        "trivy",
        "gate-engine-security-policy",
        "gate-engine-test-policy",
        "openhands-agent-server",
        "agent-server-jvm",
        "agent-server-go",
        "buildah",
    }
)
SIGNER_PLUGIN = "com.amazonaws.signer.notation.plugin"
LITELLM = "litellm"
LITELLM_MANIFEST = "deploy/kubernetes/litellm.yaml"
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
    origin: str = "compose.yaml"


@dataclass(frozen=True)
class AcliBuild:
    install: bool = False
    version: str = ""
    sha256: str = ""

    def build_args(self) -> dict[str, str]:
        if not self.install:
            return {"INSTALL_ACLI": "false"}
        return {"INSTALL_ACLI": "true", "ACLI_VERSION": self.version, "ACLI_SHA256": self.sha256}


def pinned(name: str, reference: str, origin: str) -> Mirror:
    match = _PINNED_REFERENCE.match(reference)
    if match is None:
        raise PublishError(f"{origin} pins {name} as {reference}, without a tag and digest")
    return Mirror(name, match["source"], match["tag"], match["digest"], origin)


def mirrors(compose: str) -> list[Mirror]:
    """Read the pinned upstream image of each mirrored service from `compose.yaml`."""
    services = yaml.safe_load(compose)["services"]
    return [
        pinned(name, services[service]["image"], "compose.yaml")
        for name, service in MIRRORED_SERVICES.items()
    ]


def litellm_reference(manifest: str) -> str:
    for document in yaml.safe_load_all(manifest):
        if document and document.get("kind") == "Deployment":
            for container in document["spec"]["template"]["spec"]["containers"]:
                if container["name"] == LITELLM:
                    return container["image"]
    raise PublishError(f"{LITELLM_MANIFEST} has no {LITELLM} container")


def cluster_mirrors(root: Path) -> list[Mirror]:
    """Read the pinned upstream images the EKS pods run from their settings and manifests."""
    from scripts.config import Settings

    found = [
        pinned(name, Settings.model_fields[field].default, f"the {field} setting")
        for name, field in MIRRORED_SETTINGS.items()
    ]
    manifest = (root / LITELLM_MANIFEST).read_text(encoding="utf-8")
    found.append(pinned(LITELLM, litellm_reference(manifest), LITELLM_MANIFEST))
    return found


def base_image_version(dockerfile: str, image: str) -> str:
    """Return the version of the pinned base `image` the Dockerfile's FROM line names."""
    match = re.search(rf"^FROM \S*{re.escape(image)}:([0-9.]+)@sha256:", dockerfile, re.M)
    if match is None:
        raise PublishError(f"the Dockerfile pins no {image} image by version and digest")
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
    def __init__(
        self, ecr, registry: str, name_prefix: str, arch: str, scanner=None, run=None
    ) -> None:
        self.ecr = ecr
        self.registry = registry
        self.name_prefix = name_prefix
        self.arch = arch
        self.scanner = scanner
        self.run = run or process.run
        self._logged_in = False
        self.pushed: list[str] = []
        self.refused: list[str] = []

    def _passes_scan(self, local: str, keys: list[str]) -> bool:
        """Scan an image the local Docker holds, recording it as refused when it fails."""
        if self.scanner is None:
            return True
        result = self.scanner.scan(local, keys)
        print(f"{LOG} {result.describe()}")
        if not result.passed:
            self.refused.append(result.describe())
        return result.passed

    def _local_digest(self, image: str) -> str:
        return self.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image],
            timeout=process.SHORT_COMMAND_TIMEOUT_SECONDS,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

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
            return f"{image}@{digest}"
        self._login()
        print(f"{LOG} building {image} for {PLATFORMS[self.arch]}")
        if self.scanner is None:
            self.run(
                build_command(spec, context, image, self.arch, build_args),
                timeout=BUILD_TIMEOUT_SECONDS,
                check=True,
            )
        else:
            self.run(
                build_command(spec, context, image, self.arch, build_args, output="--load"),
                timeout=BUILD_TIMEOUT_SECONDS,
                check=True,
            )
            if not self._passes_scan(image, [f"{image}@{self._local_digest(image)}"]):
                return f"{image}@refused"
            self.run(["docker", "push", image], timeout=BUILD_TIMEOUT_SECONDS, check=True)
        digest = _pushed_digest(self.ecr, repository, tag)
        self.pushed.append(f"{image}@{digest}")
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
            upstream = f"{mirror.source}@{mirror.digest}"
            if self.scanner is not None:
                self.run(
                    ["docker", "pull", "--platform", PLATFORMS[self.arch], upstream],
                    timeout=BUILD_TIMEOUT_SECONDS,
                    check=True,
                )
                if not self._passes_scan(upstream, [upstream, f"{image}@{mirror.digest}"]):
                    return f"{image}@refused"
            print(f"{LOG} copying {upstream} to {image}")
            self.run(
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
            self.pushed.append(f"{image}@{digest}")
        if digest != mirror.digest:
            raise PublishError(
                f"{image} has digest {digest}, and {mirror.origin} pins {mirror.digest}. "
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
                base_image_version(
                    (context / COLLECTOR.dockerfile).read_text(encoding="utf-8"),
                    "opentelemetry-collector-contrib",
                ),
            ),
            publisher.built(TLS_SIDECAR, context, release_version()),
            publisher.built(EKS_DEPLOYER, context, release_version()),
            publisher.built(BUILDAH, context, release_version()),
            *[
                publisher.built(spec, context / GATE_ENGINE_ROOT / name, release_version())
                for name, spec in GATE_ENGINES.items()
            ],
            *[
                publisher.built(spec, context, release_version())
                for spec in RUNTIME_IMAGES.values()
            ],
            publisher.built(
                OPA,
                context,
                base_image_version(
                    (context / OPA.dockerfile).read_text(encoding="utf-8"), "openpolicyagent/opa"
                ),
            ),
        ]
    compose = (root / "compose.yaml").read_text(encoding="utf-8")
    references += [publisher.mirror(mirror) for mirror in mirrors(compose)]
    references += [publisher.mirror(mirror) for mirror in cluster_mirrors(root)]
    return references


class Signer:
    """Sign catalog images with notation and the AWS Signer plugin inside the builder image."""

    def __init__(self, session, ecr, registry: str, profile_arn: str, arch: str, run=None) -> None:
        self.session = session
        self.ecr = ecr
        self.registry = registry
        self.profile_arn = profile_arn
        self.arch = arch
        self.run = run or process.run

    def _notation(self, builder: str, workdir: Path, *args: str) -> process.CompletedProcess:
        credentials = self.session.get_credentials().get_frozen_credentials()
        environment = {
            "AWS_ACCESS_KEY_ID": credentials.access_key,
            "AWS_SECRET_ACCESS_KEY": credentials.secret_key,
            "AWS_SESSION_TOKEN": credentials.token or "",
            "AWS_REGION": self.session.region_name,
            "AWS_USE_FIPS_ENDPOINT": "false",
            "DOCKER_CONFIG": "/auth",
        }
        env_file = workdir / "notation.env"
        env_file.write_text("".join(f"{k}={v}\n" for k, v in environment.items()))
        env_file.chmod(0o600)
        return self.run(
            [
                "docker",
                "run",
                "--rm",
                "--platform",
                PLATFORMS[self.arch],
                "--env-file",
                str(env_file),
                "--volume",
                f"{workdir / 'auth'}:/auth:ro",
                "--entrypoint",
                "notation",
                builder,
                *args,
            ],
            timeout=process.MEDIUM_COMMAND_TIMEOUT_SECONDS,
            capture_output=True,
            text=True,
        )

    def sign(self, references: list[str], builder: str) -> list[str]:
        """Sign each catalog reference that carries no signature yet, returning those it signed."""
        from services.orchestration.gates.registry_auth import ecr_token
        from services.orchestration.gates.registry_mirrors import auth_document

        account, region = self.registry.split(".", 1)[0], self.session.region_name
        signed = []
        with tempfile.TemporaryDirectory(prefix="notation-") as directory:
            workdir = Path(directory)
            (workdir / "auth").mkdir(mode=0o700)
            config = workdir / "auth" / "config.json"
            config.write_text(auth_document({self.registry: ecr_token(account, region)}))
            config.chmod(0o644)
            for reference in references:
                if image_name(reference) not in SIGNED_CATALOG:
                    continue
                listed = self._notation(builder, workdir, "list", reference)
                if listed.returncode != 0:
                    raise PublishError(f"notation could not list {reference}: {listed.stderr}")
                if "application/vnd.cncf.notary.signature" in listed.stdout:
                    print(f"{LOG} {reference} is already signed")
                    continue
                print(f"{LOG} signing {reference}")
                result = self._notation(
                    builder,
                    workdir,
                    "sign",
                    "--plugin",
                    SIGNER_PLUGIN,
                    "--id",
                    self.profile_arn,
                    reference,
                )
                if result.returncode != 0:
                    raise PublishError(f"notation could not sign {reference}: {result.stderr}")
                signed.append(reference)
        return signed


def signing_profile(explicit: str) -> str:
    """Return the profile to sign with, from the flag, IMAGE_SIGNING_PROFILE_ARN, or the root."""
    import os

    from scripts.temporal_certs import COMPOSITION_ROOT

    if explicit or os.environ.get("IMAGE_SIGNING_PROFILE_ARN"):
        return explicit or os.environ["IMAGE_SIGNING_PROFILE_ARN"]
    try:
        result = process.run(
            [
                "terraform",
                f"-chdir={COMPOSITION_ROOT}",
                "output",
                "-json",
                "image_signing_profile_arn",
            ],
            timeout=process.MEDIUM_COMMAND_TIMEOUT_SECONDS,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return ""
    if result.returncode != 0:
        return ""
    return json.loads(result.stdout or "null") or ""


def build_locally(
    images: list[tuple[ImageSpec, str, dict[str, str]]],
    arch: str,
    root: Path = REPO_ROOT,
    run=None,
) -> None:
    """Build each (spec, image, build args) into the local image store with the publish command.

    The build context and the build command are the ones a publish uses, so a cluster that runs
    these images runs what the registry would receive.
    """
    run = run or process.run
    with build_context(root) as context:
        for spec, image, build_args in images:
            print(f"{LOG} building {image} for {PLATFORMS[arch]} into the local image store")
            run(
                build_command(spec, context, image, arch, build_args, output="--load"),
                timeout=BUILD_TIMEOUT_SECONDS,
                check=True,
            )


def image_name(reference: str) -> str:
    """Return the repository suffix of a registry/prefix/name:tag@digest reference."""
    return reference.split("@", 1)[0].rsplit(":", 1)[0].rsplit("/", 1)[1]


def tfvars(references: list[str]) -> dict[str, object]:
    """Map each reference to the composition-root variable that takes it.

    The gate engine images go into one map keyed by the engine name gate_engine.yaml uses, and the
    catalog sandbox images into one keyed by the catalog name a runtime profile uses.
    """
    maps = {
        **{spec.name: (GATE_ENGINE_IMAGES_KEY, name) for name, spec in GATE_ENGINES.items()},
        **{spec.name: (RUNTIME_IMAGES_KEY, name) for name, spec in RUNTIME_IMAGES.items()},
    }
    values: dict[str, object] = {}
    for reference in references:
        name = image_name(reference)
        if name in maps:
            key, entry = maps[name]
            values.setdefault(key, {})[entry] = reference
        else:
            values[TFVARS_KEYS[name]] = reference
    return values


def registry_digest(image: str, run=None) -> str:
    """Return the digest a registry holds for `image`, the sha256 of its raw manifest."""
    run = run or process.run
    raw = run(
        ["docker", "buildx", "imagetools", "inspect", "--raw", image],
        timeout=process.MEDIUM_COMMAND_TIMEOUT_SECONDS,
        capture_output=True,
        check=True,
    ).stdout
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def push_engines_locally(registry: str, arch: str, root: Path = REPO_ROOT, run=None) -> dict:
    """Build each example gate engine, push it to a local registry, and return it by digest."""
    run = run or process.run
    pushed = {}
    with build_context(root) as context:
        for name, spec in GATE_ENGINES.items():
            engine = context / GATE_ENGINE_ROOT / name
            tag = f"{release_version()}-{content_hash(spec, engine)}-{arch}"
            image = f"{registry}/{spec.name}:{tag}"
            print(f"{LOG} building {image} for {PLATFORMS[arch]}")
            run(
                build_command(spec, engine, image, arch),
                timeout=BUILD_TIMEOUT_SECONDS,
                check=True,
            )
            pushed[name] = f"{image}@{registry_digest(image, run)}"
    return pushed


def manifest(references: list[str], pushed: list[str]) -> list[dict[str, object]]:
    """Describe each reference by repository and digest, and whether this run pushed it."""
    entries = []
    for reference in references:
        image, digest = reference.split("@", 1)
        entries.append(
            {
                "name": image_name(reference),
                "repository": image.rsplit(":", 1)[0],
                "digest": digest,
                "reference": reference,
                "pushed": reference in pushed,
            }
        )
    return entries


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name-prefix", default="agentic-platform", help="Repository prefix.")
    parser.add_argument("--arch", choices=sorted(PLATFORMS), default="arm64")
    parser.add_argument("--region", default="", help="Registry region. Defaults to AWS_REGION.")
    parser.add_argument("--install-acli", action="store_true", help="Install acli in the image.")
    parser.add_argument("--acli-version", default="")
    parser.add_argument("--acli-sha256", default="")
    parser.add_argument(
        "--images-file", type=Path, default=IMAGES_FILE, help="Where to write the tfvars JSON."
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Also describe each image and whether it was pushed.",
    )
    parser.add_argument(
        "--signing-profile",
        default="",
        help=(
            "AWS Signer profile ARN to sign the catalog images with. Defaults to "
            "IMAGE_SIGNING_PROFILE_ARN, then the composition root's image_signing_profile_arn."
        ),
    )
    parser.add_argument(
        "--unsigned",
        action="store_true",
        help="Publish without signing, for a deployment that does not verify signatures.",
    )
    parser.add_argument(
        "--block-severity",
        default="high",
        choices=("low", "medium", "high", "critical"),
        help="Lowest Inspector severity that keeps an image from being pushed.",
    )
    parser.add_argument(
        "--exceptions",
        default="",
        help="An image_exceptions.yaml whose unexpired entries accept a finding in an image.",
    )
    args = parser.parse_args(argv)

    from scripts.aws_clients import configure_aws_fips
    from scripts.config import get_settings

    profile = signing_profile(args.signing_profile)
    if not profile and not args.unsigned:
        print(
            f"{LOG} no signing profile, so nothing is published. Set IMAGE_SIGNING_PROFILE_ARN or "
            "--signing-profile to the image_signing_profile_arn output, or pass --unsigned for a "
            "deployment that does not verify signatures."
        )
        return 1

    settings = get_settings()
    configure_aws_fips(settings)

    import boto3

    region = args.region or settings.aws_region
    session = boto3.Session(region_name=region)
    account = session.client("sts").get_caller_identity()["Account"]
    registry = f"{account}.dkr.ecr.{region}.amazonaws.com"
    from scripts.image_scan import ImageScanner, load_exceptions

    scanner = ImageScanner(
        session,
        args.arch,
        block=args.block_severity,
        exceptions=load_exceptions(args.exceptions),
    )
    publisher = Publisher(session.client("ecr"), registry, args.name_prefix, args.arch, scanner)
    acli = AcliBuild(args.install_acli, args.acli_version, args.acli_sha256)
    try:
        references = publish(publisher, acli)
        if publisher.refused:
            for refusal in publisher.refused:
                print(f"{LOG} not pushed: {refusal}")
            print(
                f"{LOG} {len(publisher.refused)} image(s) carry blocking findings and were not "
                f"pushed, so {args.images_file} is unchanged. Rebuild each on a patched base, or "
                "accept each finding in the exceptions file with a reason and an expiry."
            )
            return 1
        if profile:
            builder = next(r for r in references if image_name(r) == BUILDAH.name)
            signer = Signer(session, publisher.ecr, registry, profile, args.arch)
            signer.sign(references, builder)
        else:
            print(f"{LOG} --unsigned, so the catalog images are not signed")
    except PublishError as exc:
        print(f"{LOG} {exc}")
        return 1
    for reference in references:
        print(reference)
    write_json(args.images_file, tfvars(references))
    print(f"{LOG} wrote {args.images_file}")
    if args.manifest is not None:
        write_json(args.manifest, manifest(references, publisher.pushed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
