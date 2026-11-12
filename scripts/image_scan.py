"""Scan a container image for vulnerabilities before it leaves the workstation or the CI runner.

The Amazon Inspector SBOM generator, run from `docker/image-scan.Dockerfile` because it runs only
on Linux, inventories an image archive into a CycloneDX SBOM. The Inspector Scan API evaluates
the SBOM against the vulnerability data the ECR scan uses, so an image that fails here would fail
the scan gate a tenant pod's pull passes through. Nothing is pushed.

`inv image-scan --image REF` scans any image the local Docker holds or can pull, and
`inv publish-platform-images` scans every image before it pushes it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from services import process

LOG = "[image-scan]"
REPO_ROOT = Path(__file__).resolve().parent.parent
SCANNER_NAME = "agentic-dev-platform-image-scan"
SEVERITIES = ("low", "medium", "high", "critical")
_SCAN_TIMEOUT_SECONDS = 1800

Runner = Callable[..., process.CompletedProcess[Any]]


@dataclass(frozen=True)
class Finding:
    id: str
    severity: str


@dataclass(frozen=True)
class ScanResult:
    image: str
    blocking: tuple[Finding, ...]
    accepted: tuple[Finding, ...]

    @property
    def passed(self) -> bool:
        return not self.blocking

    def describe(self) -> str:
        if self.passed:
            accepted = f", {len(self.accepted)} accepted by exception" if self.accepted else ""
            return f"{self.image} has no blocking finding{accepted}"
        listed = ", ".join(f"{f.id} ({f.severity})" for f in self.blocking[:10])
        more = len(self.blocking) - 10
        rest = f" and {more} more" if more > 0 else ""
        return f"{self.image} carries {len(self.blocking)} blocking finding(s): {listed}{rest}"


def scanner_image(arch: str, run: Runner = process.run) -> str:
    """Build the SBOM generator image into the local image store and return its reference."""
    from scripts.publish_platform_images import build_locally
    from scripts.publish_temporal_images import ImageSpec, content_hash

    spec = ImageSpec(SCANNER_NAME, "docker/image-scan.Dockerfile", ())
    image = f"{SCANNER_NAME}:{content_hash(spec, REPO_ROOT)}-{arch}"
    present = run(
        ["docker", "image", "inspect", image],
        timeout=process.SHORT_COMMAND_TIMEOUT_SECONDS,
        capture_output=True,
    )
    if present.returncode != 0:
        build_locally([(spec, image, {})], arch, REPO_ROOT, run)
    return image


class ImageScanner:
    """Inventory a local image and evaluate it with the Inspector Scan API."""

    def __init__(
        self,
        session: Any,
        arch: str,
        *,
        block: str = "high",
        exceptions: Sequence[Any] = (),
        run: Runner = process.run,
    ) -> None:
        self.session = session
        self.arch = arch
        self.blocking = frozenset(SEVERITIES[SEVERITIES.index(block.lower()) :])
        self.exceptions = tuple(exceptions)
        self.run = run
        self._scanner = ""

    def sbom(self, image: str) -> dict[str, Any]:
        """Return the CycloneDX SBOM of an image the local Docker holds."""
        if not self._scanner:
            self._scanner = scanner_image(self.arch, self.run)
        with tempfile.TemporaryDirectory(prefix="image-scan-") as directory:
            work = Path(directory)
            self.run(
                [
                    "docker",
                    "save",
                    "--platform",
                    f"linux/{self.arch}",
                    "--output",
                    str(work / "image.tar"),
                    image,
                ],
                timeout=_SCAN_TIMEOUT_SECONDS,
                check=True,
            )
            self.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--user",
                    f"{os.getuid()}:{os.getgid()}",
                    "--volume",
                    f"{work}:/work",
                    self._scanner,
                    "container",
                    "--image",
                    "/work/image.tar",
                    "--outfile",
                    "/work/sbom.json",
                    "--disable-progress-bar",
                    "--quiet",
                ],
                timeout=_SCAN_TIMEOUT_SECONDS,
                check=True,
            )
            return json.loads((work / "sbom.json").read_text(encoding="utf-8"))

    def findings(self, sbom: dict[str, Any]) -> list[Finding]:
        client = self.session.client("inspector-scan")
        document = client.scan_sbom(sbom=sbom, outputFormat="INSPECTOR")["sbom"]
        return [
            Finding(str(entry["id"]), str(entry.get("severity", "")).lower())
            for entry in document.get("vulnerabilities") or []
        ]

    def scan(self, image: str, keys: Iterable[str] = ()) -> ScanResult:
        """Scan a local image, where `keys` name the digest references an exception may cite."""
        from services.integration.image_policy import accepted, parse_reference

        references = [parse_reference(key) for key in keys]
        today = datetime.now(UTC).date()
        blocking, excepted = [], []
        for finding in sorted(set(self.findings(self.sbom(image))), key=lambda f: f.id):
            if finding.severity not in self.blocking:
                continue
            if any(accepted(self.exceptions, ref, finding.id, today) for ref in references):
                excepted.append(finding)
            else:
                blocking.append(finding)
        return ScanResult(image, tuple(blocking), tuple(excepted))


def load_exceptions(path: str) -> tuple[Any, ...]:
    if not path:
        return ()
    import yaml
    from services.integration.image_policy import parse_exceptions

    return parse_exceptions(yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", action="append", required=True, help="Image to scan.")
    parser.add_argument("--arch", default="arm64", help="Platform of the image to scan.")
    parser.add_argument(
        "--block-severity", default="high", choices=SEVERITIES, help="Lowest blocking severity."
    )
    parser.add_argument("--exceptions", default="", help="An image_exceptions.yaml to honour.")
    parser.add_argument("--region", default="", help="Inspector region. Defaults to AWS_REGION.")
    args = parser.parse_args(argv)

    import boto3

    from scripts.config import get_settings

    session = boto3.Session(region_name=args.region or get_settings().aws_region)
    scanner = ImageScanner(
        session,
        args.arch,
        block=args.block_severity,
        exceptions=load_exceptions(args.exceptions),
    )
    failed = 0
    for image in args.image:
        result = scanner.scan(image, [image])
        print(f"{LOG} {result.describe()}")
        failed += 0 if result.passed else 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
