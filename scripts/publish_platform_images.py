"""Build the platform container images and the third-party components, and push them to ECR.

The ECS tasks and the EKS pods pull only from ECR, because DNS Firewall resolves no public
registry.

- The platform image carries the Temporal Worker Deployment build ID, a release version and a
  UTC timestamp that `scripts.build_id` orders. Its tag is `<build id>-<source hash>-<arch>`,
  so a build whose sources are already in the repository reuses that image and its build ID
  instead of minting a new Worker Deployment version.
- The repository-intelligence, collector, TLS sidecar, OPA, eks-deployer, and buildah builder
  images, the example gate engine images under `examples/gate-engines`, and the catalog sandbox
  images `agent-server-jvm`, `agent-server-go`, and `checks-*` are tagged
  `<version>-<content hash>-<arch>`.
- trivy, semgrep, ClickHouse, LiteLLM, and Langfuse web and worker are built from the versions
  `docker/al2023/versions.env` pins and tagged `<version>-<content hash>-<arch>`. A component whose
  `<NAME>_IMAGE_MODE` pin is `release` is copied from its pinned upstream image instead, tagged
  with its upstream version, and must keep the pinned digest.

The images are built from the files git tracks or would track, so ignored local artifacts such
as the binaries under `vendor/bin` never reach an image.

The references are written to `images.auto.tfvars.json` in the `private-bedrock` composition
root, which Terraform loads with no flag, and optionally to a manifest that marks the images
this run pushed.

Every image whose Dockerfile starts FROM ${AL2023_IMAGE} builds on the hardened base that
`scripts.al2023_base` describes. Every image is scanned with the Amazon Inspector SBOM generator
and the Inspector Scan API, as `scripts.image_scan` describes, and against the CIS benchmark
before it is pushed, and the results of each CIS scan are written under `--cis-evidence`. An
image with an unaccepted finding at `--block-severity` or above, or a CIS rule that neither passes
nor is accepted, is not pushed, and the run writes no references.

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
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from services import process

from scripts import base_versions
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
        "docker/al2023/go-component.sh",
    ),
)
COLLECTOR = ImageSpec(
    "otel-collector",
    "docker/otel-collector.Dockerfile",
    ("docker/healthprobe", "docker/al2023/go-component.sh"),
)
OPA = ImageSpec(
    "opa", "docker/opa.Dockerfile", ("docker/healthprobe", "docker/al2023/go-component.sh")
)
EKS_DEPLOYER = ImageSpec(
    "eks-deployer",
    "docker/eks-deployer.Dockerfile",
    (
        "docker/al2023/go-component.sh",
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
BUILDAH = ImageSpec("buildah", "docker/buildah.Dockerfile", ("docker/al2023/go-component.sh",))
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
TRIVY = ImageSpec("trivy", "docker/trivy.Dockerfile", ("docker/al2023/go-component.sh",))
SEMGREP = ImageSpec("semgrep", "docker/semgrep.Dockerfile", ("docker/semgrep",))
CLICKHOUSE = ImageSpec("clickhouse", "docker/clickhouse.Dockerfile", ())
LITELLM = ImageSpec("litellm", "docker/litellm.Dockerfile", ("docker/litellm",))
LANGFUSE_WEB = ImageSpec(
    "langfuse-web",
    "docker/langfuse.Dockerfile",
    ("docker/al2023/go-component.sh",),
    target="langfuse-web",
)
LANGFUSE_WORKER = ImageSpec(
    "langfuse-worker",
    "docker/langfuse.Dockerfile",
    ("docker/al2023/go-component.sh",),
    target="langfuse-worker",
)
BUILD = "build"
RELEASE = "release"
# Third-party images the platform builds, or mirrors from the pinned upstream image when the
# component's <NAME>_IMAGE_MODE pin in docker/al2023/versions.env is release.
IMAGE_COMPONENTS = {
    "trivy": (TRIVY, "TRIVY_UPSTREAM_IMAGE"),
    "semgrep": (SEMGREP, "SEMGREP_UPSTREAM_IMAGE"),
    "clickhouse": (CLICKHOUSE, "CLICKHOUSE_IMAGE"),
    "litellm": (LITELLM, "LITELLM_UPSTREAM_IMAGE"),
    "langfuse-web": (LANGFUSE_WEB, "LANGFUSE_WEB_UPSTREAM_IMAGE"),
    "langfuse-worker": (LANGFUSE_WORKER, "LANGFUSE_WORKER_UPSTREAM_IMAGE"),
}
AGENT_SERVER_DOCKERFILE = "docker/agent-server.Dockerfile"
AGENT_SERVER_INPUTS = ("docker/agent-server", "docker/al2023/go-component.sh")
# The repository keeps the name the mirrored upstream image used, so its signatures and the
# eks_agent_server_image variable carry over.
AGENT_SERVER = ImageSpec(
    "openhands-agent-server",
    AGENT_SERVER_DOCKERFILE,
    AGENT_SERVER_INPUTS,
    target="agent-server-python-node",
)
CHECKS_DOCKERFILE = "docker/checks.Dockerfile"
# The catalog images beside the agent-server image that runtime profiles name as catalog:<name>.
RUNTIME_IMAGES = {
    **{
        name: ImageSpec(name, AGENT_SERVER_DOCKERFILE, AGENT_SERVER_INPUTS, target=name)
        for name in ("agent-server-jvm", "agent-server-go")
    },
    **{
        name: ImageSpec(name, CHECKS_DOCKERFILE, (), target=name)
        for name in ("checks-python", "checks-node", "checks-jvm", "checks-go")
    },
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
        "checks-python",
        "checks-node",
        "checks-jvm",
        "checks-go",
        "buildah",
    }
)
SIGNER_PLUGIN = "com.amazonaws.signer.notation.plugin"
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


def pinned(name: str, reference: str, origin: str) -> Mirror:
    match = _PINNED_REFERENCE.match(reference)
    if match is None:
        raise PublishError(f"{origin} pins {name} as {reference}, without a tag and digest")
    return Mirror(name, match["source"], match["tag"], match["digest"], origin)


def component_mode(pins: Mapping[str, str], name: str) -> str:
    return pins.get(f"{name.upper().replace('-', '_')}_IMAGE_MODE", BUILD)


def release_mirrors(pins: Mapping[str, str]) -> list[Mirror]:
    """Return the upstream image of each component whose IMAGE_MODE pin is release."""
    return [
        pinned(name, pins[upstream], "docker/al2023/versions.env")
        for name, (_, upstream) in IMAGE_COMPONENTS.items()
        if component_mode(pins, name) == RELEASE
    ]


def base_image_version(dockerfile: str, image: str) -> str:
    """Return the version of the pinned `image` a FROM line or an ARG default names."""
    match = re.search(
        rf"^(?:FROM \S*|ARG \w+=\S*){re.escape(image)}:([0-9.]+)@sha256:", dockerfile, re.M
    )
    if match is None:
        raise PublishError(f"the Dockerfile pins no {image} image by version and digest")
    return match.group(1)


def pinned_version(dockerfile: str, name: str) -> str:
    """Return the version an `ARG <name>=v<version>` default pins, without its leading v."""
    match = re.search(rf"^ARG {re.escape(name)}=v?([0-9][0-9.]*)$", dockerfile, re.M)
    if match is None:
        raise PublishError(f"the Dockerfile pins no {name}")
    return match.group(1)


def platform_source_hash(root: Path, arch: str) -> str:
    digest = hashlib.sha256(source_digest(root).encode())
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
        self,
        ecr,
        registry: str,
        name_prefix: str,
        arch: str,
        scanner=None,
        run=None,
        log=LOG,
        base=None,
    ) -> None:
        self.log = log
        self.base = base
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
        print(f"{self.log} {result.describe()}")
        for line in result.detail():
            print(f"{self.log}   {line}")
        if not result.passed:
            self.refused.append(result.describe())
        return result.passed

    def _passes_cis(self, context: Path, image: str, name: str) -> bool:
        """Scan an image against the CIS benchmark, recording it as refused when it fails."""
        if self.base is None:
            return True
        problems = self.base.scan(context, image, name)
        print(f"{self.log} {image} {'fails' if problems else 'passes'} the CIS benchmark scan")
        for problem in problems:
            print(f"{self.log}   {problem}")
        if problems:
            self.refused.append(f"{image} fails {len(problems)} CIS benchmark check(s)")
        return not problems

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
            print(f"{self.log} {image} is already pushed")
            return f"{image}@{digest}"
        self._login()
        if self.base is not None:
            dockerfile = (context / spec.dockerfile).read_text(encoding="utf-8")
            build_args = {**self.base.build_args(context, dockerfile), **(build_args or {})}
        print(f"{self.log} building {image} for {PLATFORMS[self.arch]}")
        if self.scanner is None and self.base is None:
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
            if not self._passes_cis(context, image, spec.name):
                return f"{image}@refused"
            self.run(["docker", "push", image], timeout=BUILD_TIMEOUT_SECONDS, check=True)
        digest = _pushed_digest(self.ecr, repository, tag)
        self.pushed.append(f"{image}@{digest}")
        return f"{image}@{digest}"

    def platform(self, context: Path) -> str:
        repository = self._repository(PLATFORM.name)
        source_hash = platform_source_hash(context, self.arch)
        tag, digest = published_platform_tag(self.ecr, repository, source_hash, self.arch)
        if tag:
            image = f"{self.registry}/{repository}:{tag}"
            print(f"{self.log} {image} is already pushed, build ID {build_id_of(tag)}")
            return f"{image}@{digest}"
        build_id = generate_build_id()
        build_args = {"AGENT_BUILD_ID": build_id}
        return self._build(PLATFORM, context, f"{build_id}-{source_hash}-{self.arch}", build_args)

    def built(self, spec: ImageSpec, context: Path, version: str) -> str:
        return self._build(spec, context, f"{version}-{content_hash(spec, context)}-{self.arch}")

    def mirror(self, mirror: Mirror) -> str:
        repository = self._repository(mirror.name)
        image = f"{self.registry}/{repository}:{mirror.tag}"
        digest = _pushed_digest(self.ecr, repository, mirror.tag)
        if digest:
            print(f"{self.log} {image} is already pushed")
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
            print(f"{self.log} copying {upstream} to {image}")
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


def publish(publisher: Publisher, root: Path = REPO_ROOT) -> list[str]:
    """Push every image whose tag is not yet in its repository, returning each reference."""
    pins = base_versions.load(root / "docker" / "al2023" / "versions.env")
    with build_context(root) as context:
        references = [
            publisher.platform(context),
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
            publisher.built(AGENT_SERVER, context, release_version()),
            publisher.built(
                OPA,
                context,
                pinned_version(
                    (context / OPA.dockerfile).read_text(encoding="utf-8"), "OPA_VERSION"
                ),
            ),
            *[
                publisher.built(spec, context, release_version())
                for name, (spec, _) in IMAGE_COMPONENTS.items()
                if component_mode(pins, name) != RELEASE
            ],
        ]
    references += [publisher.mirror(mirror) for mirror in release_mirrors(pins)]
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

    The build context, the hardened base, and the build command are the ones a publish uses, so a
    cluster that runs these images runs what the registry would receive.
    """
    from scripts.al2023_base import Base

    run = run or process.run
    base = Base(arch, run)
    with build_context(root) as context:
        for spec, image, build_args in images:
            dockerfile = (context / spec.dockerfile).read_text(encoding="utf-8")
            arguments = {**base.build_args(context, dockerfile), **build_args}
            print(f"{LOG} building {image} for {PLATFORMS[arch]} into the local image store")
            run(
                build_command(spec, context, image, arch, arguments, output="--load"),
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


def push_locally(
    images: Mapping[str, tuple[ImageSpec, Path]], registry: str, arch: str, run=None
) -> dict[str, str]:
    """Build each (spec, context) on the hardened base, push it to a registry, and pin it.

    Returns each image by its key, as `<registry>/<spec name>:<tag>@<digest>`.
    """
    from scripts.al2023_base import Base

    run = run or process.run
    base = Base(arch, run)
    pushed = {}
    for key, (spec, context) in images.items():
        tag = f"{release_version()}-{content_hash(spec, context)}-{arch}"
        image = f"{registry}/{spec.name}:{tag}"
        dockerfile = (context / spec.dockerfile).read_text(encoding="utf-8")
        print(f"{LOG} building {image} for {PLATFORMS[arch]}")
        run(
            build_command(spec, context, image, arch, base.build_args(context, dockerfile)),
            timeout=BUILD_TIMEOUT_SECONDS,
            check=True,
        )
        pushed[key] = f"{image}@{registry_digest(image, run)}"
    return pushed


def push_engines_locally(registry: str, arch: str, root: Path = REPO_ROOT, run=None) -> dict:
    """Build each example gate engine, push it to a local registry, and return it by digest."""
    with build_context(root) as context:
        engines = {
            name: (spec, context / GATE_ENGINE_ROOT / name) for name, spec in GATE_ENGINES.items()
        }
        return push_locally(engines, registry, arch, run)


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
    from services.integration.image_exceptions import platform_exceptions

    from scripts.image_scan import ImageScanner

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
        base=Base(args.arch, evidence=args.cis_evidence),
    )
    try:
        references = publish(publisher)
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
