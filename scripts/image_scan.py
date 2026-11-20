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
from dataclasses import dataclass, field
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
    # Each affected component as "path installed fixed", which locates the finding in the image.
    affects: tuple[str, ...] = field(default=(), compare=False)


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

    def detail(self) -> list[str]:
        """Return one line per blocking finding and affected component."""
        return [
            f"{finding.id} ({finding.severity}) {affected}".rstrip()
            for finding in self.blocking
            for affected in finding.affects or ("",)
        ]


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
            Finding(
                str(entry["id"]),
                str(entry.get("severity", "")).lower(),
                tuple(_affected(item) for item in entry.get("affects") or ()),
            )
            for entry in document.get("vulnerabilities") or []
        ]

    def scan(self, image: str, keys: Iterable[str] = ()) -> ScanResult:
        """Scan a local image, where `keys` name the digest references an exception may cite."""
        from services.integration.image_policy import accepted, parse_reference

        references = [parse_reference(key) for key in keys]
        today = datetime.now(UTC).date()
        blocking, excepted = [], []
        for finding in _merged(self.findings(self.sbom(image))):
            if finding.severity not in self.blocking:
                continue
            if any(accepted(self.exceptions, ref, finding.id, today) for ref in references):
                excepted.append(finding)
            else:
                blocking.append(finding)
        return ScanResult(image, tuple(blocking), tuple(excepted))


def _merged(findings: Iterable[Finding]) -> list[Finding]:
    """Return one finding per ID and severity, with every component it affects."""
    merged: dict[tuple[str, str], list[str]] = {}
    for finding in findings:
        affects = merged.setdefault((finding.id, finding.severity), [])
        affects.extend(item for item in finding.affects if item not in affects)
    return [Finding(i, s, tuple(a)) for (i, s), a in sorted(merged.items())]


def _affected(item: dict[str, Any]) -> str:
    if "file" in item:
        # A Dockerfile finding names the build instruction, not a package.
        return f"{item['file']} lines {item.get('lines', '')}".strip()
    installed = str(item.get("installed_version", ""))
    fixed = str(item.get("fixed_version", "") or "no fix")
    return " ".join(
        part for part in (str(item.get("path", "")), installed, f"fixed in {fixed}") if part
    )


def load_exceptions(path: str, scope: str = "platform") -> tuple[Any, ...]:
    """Parse an image_exceptions.yaml with the parser the pull gate and the review share."""
    if not path:
        return ()
    from services.integration.image_exceptions import load_file

    return load_file(path, scope)


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
        for line in result.detail():
            print(f"{LOG}   {line}")
        failed += 0 if result.passed else 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
