"""Build the hardened Amazon Linux 2023 base and scan images against the CIS benchmark.

docker/al2023/base.Dockerfile applies the CIS Amazon Linux 2023 Level 2 Server remediation to the
pinned minimal image. The base is built into the local image store once per content hash of its
inputs, after its own scan passes, and every Dockerfile that starts FROM ${AL2023_IMAGE} receives
it as that build argument. The Dockerfiles keep the minimal image as the default, so a plain
docker build still works but is not hardened.

A scan mounts the image's root file system into the OpenSCAP stage of the same Dockerfile, so the
scanner never enters the image, and writes the ARF results, the HTML report, the per-rule results,
and the blocking rules to the evidence directory. It also checks the PATH in the image's
configuration, which the benchmark's root_path_no_dot rule cannot read offline.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from services import process

from scripts.build_id import release_version
from scripts.publish_temporal_images import (
    BUILD_TIMEOUT_SECONDS,
    HARDENED_BASE_FILES,
    PLATFORMS,
    ImageSpec,
    _pushed_digest,
    base_root,
    build_command,
    content_hash,
)

BASE_ARG = "AL2023_IMAGE"
BASE_DOCKERFILE, *_inputs = HARDENED_BASE_FILES
BASE_INPUTS = tuple(_inputs)
BASE = ImageSpec("al2023-base", BASE_DOCKERFILE, BASE_INPUTS, target="base")
SCANNER = ImageSpec("al2023-openscap", BASE_DOCKERFILE, BASE_INPUTS, target="openscap")
LOCAL_PREFIX = "agentic-dev-platform-"
LOG = "[al2023-base]"


def uses_base(dockerfile: str) -> bool:
    return "${" + BASE_ARG + "}" in dockerfile


def path_problems(path: str) -> list[str]:
    """Return why a PATH value holds an empty or relative entry, the root_path_no_dot rule."""
    return [
        f"PATH entry {entry!r} is {'empty' if not entry else 'relative'}"
        for entry in path.split(":")
        if not entry.startswith("/")
    ]


class Base:
    """The hardened base and the OpenSCAP scanner of one build context and architecture."""

    def __init__(
        self, arch: str, run=None, evidence: Path | None = None, record_base: bool = True
    ) -> None:
        self.arch = arch
        self.run = run or process.run
        self.evidence = evidence
        self.record_base = record_base
        self._base: str | None = None

    def _local(self, spec: ImageSpec, context: Path) -> str:
        tag = f"{release_version()}-{content_hash(spec, context)}-{self.arch}"
        return f"{LOCAL_PREFIX}{spec.name}:{tag}"

    def _present(self, image: str) -> bool:
        return (
            self.run(
                ["docker", "image", "inspect", image],
                timeout=process.SHORT_COMMAND_TIMEOUT_SECONDS,
                capture_output=True,
                check=False,
            ).returncode
            == 0
        )

    def _load(self, spec: ImageSpec, context: Path) -> str:
        image = self._local(spec, context)
        if not self._present(image):
            print(f"{LOG} building {image} for {PLATFORMS[self.arch]}")
            self.run(
                build_command(spec, context, image, self.arch, output="--load"),
                timeout=BUILD_TIMEOUT_SECONDS,
                check=True,
            )
        return image

    def reference(self, context: Path) -> str:
        """Return the local base image, scanning and building it first when it is absent.

        With an evidence directory the scan runs every time, from the build cache when nothing
        changed, so each publish records the base's results.
        """
        context = base_root(context)
        if self._base is None:
            image = self._local(BASE, context)
            if (self.evidence is not None and self.record_base) or not self._present(image):
                self._verify(context)
            self._base = self._load(BASE, context)
        return self._base

    def _evidence_dir(self, name: str) -> Path | None:
        if self.evidence is None:
            return None
        directory = self.evidence / name
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _verify(self, context: Path) -> None:
        with tempfile.TemporaryDirectory(prefix="al2023-base-evidence-") as directory:
            destination = self._evidence_dir(BASE.name) or Path(directory)
            print(f"{LOG} scanning the base against the CIS benchmark")
            self.run(
                [
                    "docker",
                    "buildx",
                    "build",
                    "--platform",
                    PLATFORMS[self.arch],
                    "--provenance=false",
                    "--file",
                    str(context / BASE_DOCKERFILE),
                    "--target",
                    "evidence",
                    "--output",
                    f"type=local,dest={destination}",
                    str(context),
                ],
                timeout=BUILD_TIMEOUT_SECONDS,
                check=True,
            )

    def _shared(self, spec: ImageSpec, context: Path, registry: str, name_prefix: str) -> str:
        local = self._local(spec, context)
        return f"{registry}/{name_prefix}/{spec.name}:{local.split(':', 1)[1]}"

    def share(self, context: Path, ecr, registry: str, name_prefix: str) -> None:
        """Build, scan, and push the base and the scanner, so parallel publish jobs reuse them.

        Both are build tools rather than platform images, so each is tagged with the content
        hash of the base inputs, and a tag the repository already holds is not pushed again.
        """
        context = base_root(context)
        self.reference(context)
        self._load(SCANNER, context)
        for spec in (BASE, SCANNER):
            shared = self._shared(spec, context, registry, name_prefix)
            repository, tag = shared.removeprefix(f"{registry}/").split(":", 1)
            if _pushed_digest(ecr, repository, tag):
                print(f"{LOG} {shared} is already pushed")
                continue
            self.run(
                ["docker", "tag", self._local(spec, context), shared],
                timeout=process.SHORT_COMMAND_TIMEOUT_SECONDS,
                check=True,
            )
            print(f"{LOG} pushing {shared}")
            self.run(["docker", "push", shared], timeout=BUILD_TIMEOUT_SECONDS, check=True)

    def seed(self, context: Path, registry: str, name_prefix: str) -> None:
        """Pull the base and the scanner another job shared, under the local names a build uses."""
        context = base_root(context)
        for spec in (BASE, SCANNER):
            shared = self._shared(spec, context, registry, name_prefix)
            self.run(
                ["docker", "pull", "--platform", PLATFORMS[self.arch], shared],
                timeout=BUILD_TIMEOUT_SECONDS,
                check=True,
            )
            self.run(
                ["docker", "tag", shared, self._local(spec, context)],
                timeout=process.SHORT_COMMAND_TIMEOUT_SECONDS,
                check=True,
            )

    def build_args(self, context: Path, dockerfile: str) -> dict[str, str]:
        """Return the build argument that starts a Dockerfile FROM the base, when it uses one."""
        return {BASE_ARG: self.reference(context)} if uses_base(dockerfile) else {}

    def scan(self, context: Path, image: str, name: str) -> list[str]:
        """Scan a local image against the CIS benchmark, returning the blocking problems."""
        scanner = self._load(SCANNER, base_root(context))
        destination = self._evidence_dir(name)
        with tempfile.TemporaryDirectory(prefix="cis-scan-") as directory:
            output = destination or Path(directory) / "evidence"
            dockerfile = Path(directory) / "Dockerfile"
            dockerfile.write_text(
                f"FROM {scanner} AS scan\n"
                f"RUN --mount=from={image},target=/target,readwrite "
                "cis-scan /target /evidence || true\n"
                "FROM scratch\n"
                "COPY --from=scan /evidence /\n",
                encoding="utf-8",
            )
            self.run(
                [
                    "docker",
                    "buildx",
                    "build",
                    "--platform",
                    PLATFORMS[self.arch],
                    "--provenance=false",
                    "--file",
                    str(dockerfile),
                    "--output",
                    f"type=local,dest={output}",
                    directory,
                ],
                timeout=BUILD_TIMEOUT_SECONDS,
                check=True,
            )
            results = output / "results.tsv"
            if not results.is_file() or not results.read_text(encoding="utf-8").strip():
                return [f"{image}: the CIS scan reported no rule results"]
            blocking = [
                f"{image}: CIS rule {line.replace(chr(9), ' ')}"
                for line in (output / "blocking.tsv").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        return blocking + [f"{image}: {problem}" for problem in self._path_problems(image)]

    def _path_problems(self, image: str) -> list[str]:
        inspected = self.run(
            ["docker", "image", "inspect", "--format", "{{json .Config.Env}}", image],
            timeout=process.SHORT_COMMAND_TIMEOUT_SECONDS,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        variables = dict(
            entry.split("=", 1) for entry in json.loads(inspected or "null") or [] if "=" in entry
        )
        return path_problems(variables["PATH"]) if "PATH" in variables else []
