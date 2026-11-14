"""Report the newest upstream release of each component `docker/al2023/versions.env` pins.

A component built from source, or an image the platform builds instead of copying, returns to
the upstream release once that release scans clean. `check` reads the newest release of each
pinned component from the GitHub releases API, or from go.dev for Go, and writes the result with
the upstream images the daily `publish-images` run scans with `inv image-scan`. `report` renders
the result and those scans as markdown. A release the check cannot read is recorded with its
error and never fails the run.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from scripts import base_versions

GO_FEED = "go.dev"
GITHUB_API = "https://api.github.com"
GO_DOWNLOADS = "https://go.dev/dl/?mode=json"
BUILD = "build"
SOURCE = "source"
CLEAN = "clean"
FINDINGS = "findings"
UNAVAILABLE = "unavailable"
ERROR = "error"
_FETCH_TIMEOUT_SECONDS = 20.0
_VERSION = re.compile(r"\d+(?:\.\d+)+")
_FINDING_LINE = re.compile(r"^\[image-scan\] {3}(?P<id>\S+) \((?:low|medium|high|critical)\)", re.M)

Fetch = Callable[[str], Any]


@dataclass(frozen=True)
class Component:
    name: str
    repository: str
    # The versions.env key that holds the version, or an image reference tagged with it.
    pin: str
    # <prefix>_MODE selects release or source, and <prefix>_IMAGE_MODE selects release or build.
    prefix: str = ""
    image_pin: str = ""

    @property
    def upstream_image_pin(self) -> str:
        return self.image_pin or (f"{self.prefix}_UPSTREAM_IMAGE" if self.prefix else "")


COMPONENTS = (
    Component("al2023", "amazonlinux/amazon-linux-2023", "AL2023_RELEASE"),
    Component("go", GO_FEED, "GO_VERSION"),
    Component("uv", "astral-sh/uv", "UV_IMAGE"),
    Component("kubectl", "kubernetes/kubernetes", "KUBECTL_VERSION", "KUBECTL"),
    Component("helm", "helm/helm", "HELM_VERSION", "HELM"),
    Component(
        "aws-iam-authenticator",
        "kubernetes-sigs/aws-iam-authenticator",
        "AWS_IAM_AUTHENTICATOR_VERSION",
        "AWS_IAM_AUTHENTICATOR",
    ),
    Component("opa", "open-policy-agent/opa", "OPA_VERSION", "OPA"),
    Component(
        "otel-collector", "open-telemetry/opentelemetry-collector-releases", "OTEL_COLLECTOR_IMAGE"
    ),
    Component("notation", "notaryproject/notation", "NOTATION_VERSION", "NOTATION"),
    Component("scip", "sourcegraph/scip", "SCIP_VERSION", "SCIP"),
    Component("terraform-ls", "hashicorp/terraform-ls", "TERRAFORM_LS_VERSION", "TERRAFORM_LS"),
    Component("scip-java", "sourcegraph/scip-java", "SCIP_JAVA_VERSION"),
    Component("scip-ruby", "sourcegraph/scip-ruby", "SCIP_RUBY_VERSION"),
    Component("scip-python", "sourcegraph/scip-python", "SCIP_PYTHON_VERSION"),
    Component("scip-typescript", "sourcegraph/scip-typescript", "SCIP_TYPESCRIPT_VERSION"),
    Component("phpactor", "phpactor/phpactor", "PHPACTOR_VERSION"),
    Component("ast-grep", "ast-grep/ast-grep", "AST_GREP_VERSION"),
    Component("tini", "krallin/tini", "TINI_VERSION"),
    Component("dumb-init", "Yelp/dumb-init", "DUMB_INIT_VERSION"),
    Component("golang-migrate", "golang-migrate/migrate", "GOLANG_MIGRATE_VERSION"),
    Component("rust", "rust-lang/rust", "RUST_VERSION"),
    Component("cargo-about", "EmbarkStudios/cargo-about", "CARGO_ABOUT_VERSION"),
    Component("cargo-auditable", "rust-secure-code/cargo-auditable", "CARGO_AUDITABLE_VERSION"),
    Component("openscap", "OpenSCAP/openscap", "OPENSCAP_VERSION"),
    Component("scap-security-guide", "ComplianceAsCode/content", "SSG_VERSION"),
    Component("trivy", "aquasecurity/trivy", "TRIVY_VERSION", "TRIVY"),
    Component("semgrep", "semgrep/semgrep", "SEMGREP_UPSTREAM_IMAGE", "SEMGREP"),
    Component(
        "clickhouse", "ClickHouse/ClickHouse", "CLICKHOUSE_IMAGE", "CLICKHOUSE", "CLICKHOUSE_IMAGE"
    ),
    Component("litellm", "BerriAI/litellm", "LITELLM_VERSION", "LITELLM"),
    Component("langfuse-web", "langfuse/langfuse", "LANGFUSE_VERSION", "LANGFUSE_WEB"),
    Component("langfuse-worker", "langfuse/langfuse", "LANGFUSE_VERSION", "LANGFUSE_WORKER"),
    Component("temporal", "temporalio/temporal", "TEMPORAL_VERSION"),
    Component("temporal-cli", "temporalio/cli", "TEMPORAL_CLI_VERSION"),
    Component("temporal-ui", "temporalio/ui", "TEMPORAL_UI_VERSION"),
)


@dataclass(frozen=True)
class Release:
    component: str
    repository: str
    pin: str
    pinned: str
    newest: str = ""
    # The pin's value at the newest release, in the pin's own format.
    target: str = ""
    url: str = ""
    newer: bool = False
    prefix: str = ""
    mode: str = ""
    image_mode: str = ""
    upstream_image: str = ""
    image_pin: str = ""
    # The upstream image the run scans, at the newest release when one is known.
    scan_image: str = ""
    error: str = ""

    def advice(self) -> str:
        if self.error:
            return f"The release check failed. {self.error}"
        if _key(self.newest) is None or _key(self.pinned) is None:
            return f"{self.pinned} and {self.newest or 'the release'} do not compare as versions."
        built = [
            label
            for label, mode in (
                ("binary", self.mode == SOURCE),
                ("image", self.image_mode == BUILD),
            )
            if mode
        ]
        if not self.newer:
            if built:
                return (
                    f"The pin is the newest release. The platform builds its {' and '.join(built)}."
                )
            return "The pin is the newest release."
        steps = []
        if self.mode == SOURCE:
            steps.append(
                f"Try {self.prefix}_MODE=release with {self.pin}={self.target}, which installs "
                "the upstream binary, and keep it if the image scans clean."
            )
        if self.image_mode == BUILD:
            steps.append(
                f"Try {self.prefix}_IMAGE_MODE=release if the scan of the upstream image below "
                "is clean."
            )
        if not steps:
            steps.append(
                f"Raise {self.pin} to {self.target or self.newest} and refresh the checksums "
                "or digest pinned with it."
            )
        return " ".join(["A newer release exists.", *steps])


@dataclass(frozen=True)
class Scan:
    image: str
    status: str
    findings: tuple[str, ...] = ()
    # The digest reference the pull resolved, which a release-mode pin names.
    resolved: str = ""
    log: str = ""

    @property
    def completed(self) -> bool:
        return self.status in (CLEAN, FINDINGS)

    def describe(self) -> str:
        if self.status == CLEAN:
            return f"Clean at {self.resolved or self.image}."
        if self.status == FINDINGS:
            listed = ", ".join(self.findings[:10])
            more = len(self.findings) - 10
            rest = f" and {more} more" if more > 0 else ""
            return f"{len(self.findings)} blocking finding(s), {listed}{rest}."
        if self.status == UNAVAILABLE:
            return f"Not scanned, because the pull failed. See `{self.log}`."
        return f"The scan failed. See `{self.log}`."


def version_of(text: str) -> str:
    """Return the dotted version a tag or pin carries, such as 1.37.2 from v1.37.2."""
    match = _VERSION.search(text)
    return match.group(0) if match else ""


def _key(text: str) -> tuple[int, ...] | None:
    version = version_of(text)
    return tuple(int(part) for part in version.split(".")) if version else None


def is_newer(newest: str, pinned: str) -> bool:
    newest_key, pinned_key = _key(newest), _key(pinned)
    return newest_key is not None and pinned_key is not None and newest_key > pinned_key


def _image_parts(reference: str) -> tuple[str, str]:
    """Return the name and tag of an image reference, dropping any digest."""
    remainder = reference.partition("@")[0]
    name, _, tag = remainder.rpartition(":")
    if not name or "/" in tag:
        return remainder, ""
    return name, tag


def pinned_version(value: str) -> str:
    return _image_parts(value)[1] if "/" in value else value


def newer_image(pinned_reference: str, newest: str) -> str:
    """Return the upstream image tagged like the pin but at the newest version."""
    name, tag = _image_parts(pinned_reference)
    current, latest = version_of(tag), version_of(newest)
    if not current or not latest:
        return ""
    return f"{name}:{tag.replace(current, latest, 1)}"


def retarget(value: str, newest: str) -> str:
    """Return a pin's value at the newest version, an image pin without its digest."""
    if "/" in value:
        return newer_image(value, newest)
    current, latest = version_of(value), version_of(newest)
    return value.replace(current, latest, 1) if current and latest else ""


def newest_release(component: Component, fetch: Fetch) -> tuple[str, str]:
    """Return the newest stable release tag of a component and the page that describes it."""
    if component.repository == GO_FEED:
        for release in fetch(GO_DOWNLOADS):
            if release.get("stable"):
                version = str(release["version"])
                return version, f"https://go.dev/doc/devel/release#{version}"
        raise ValueError("go.dev lists no stable release")
    document = fetch(f"{GITHUB_API}/repos/{component.repository}/releases/latest")
    return str(document["tag_name"]), str(document.get("html_url", ""))


def check(
    pins: Mapping[str, str], fetch: Fetch, components: Iterable[Component] = COMPONENTS
) -> list[Release]:
    """Compare each pinned component with its newest upstream release."""
    releases = []
    for component in components:
        if component.pin not in pins:
            continue
        pinned = pinned_version(pins[component.pin])
        prefix = component.prefix
        image_mode = pins.get(f"{prefix}_IMAGE_MODE", "") if prefix else ""
        upstream = pins.get(component.upstream_image_pin, "") if image_mode else ""
        common = {
            "component": component.name,
            "repository": component.repository,
            "pin": component.pin,
            "pinned": pinned,
            "prefix": prefix,
            "mode": pins.get(f"{prefix}_MODE", "") if prefix else "",
            "image_mode": image_mode,
            "upstream_image": upstream,
            "image_pin": component.upstream_image_pin if image_mode else "",
        }
        try:
            newest, url = newest_release(component, fetch)
        except Exception as exc:
            scan_image = upstream if image_mode == BUILD else ""
            releases.append(Release(**common, scan_image=scan_image, error=_one_line(exc)))
            continue
        newer = is_newer(newest, pinned)
        target = retarget(pins[component.pin], newest)
        scan_image = ""
        if image_mode == BUILD and upstream:
            scan_image = (newer and newer_image(upstream, newest)) or upstream
        releases.append(
            Release(
                **common, newest=newest, target=target, url=url, newer=newer, scan_image=scan_image
            )
        )
    return releases


def _one_line(exc: BaseException) -> str:
    text = " ".join(str(exc).split()) or type(exc).__name__
    return text if text.endswith(".") else f"{text}."


def fetch_json(url: str) -> Any:
    import httpx

    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token and url.startswith(GITHUB_API):
        headers["Authorization"] = f"Bearer {token}"
    response = httpx.get(
        url, headers=headers, timeout=_FETCH_TIMEOUT_SECONDS, follow_redirects=True
    )
    response.raise_for_status()
    return response.json()


def scan_targets(releases: Iterable[Release]) -> list[dict[str, str]]:
    return [{"name": r.component, "image": r.scan_image} for r in releases if r.scan_image]


def write_releases(path: Path, releases: Iterable[Release]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(r) for r in releases], indent=2) + "\n", encoding="utf-8")


def read_releases(path: Path) -> list[Release]:
    if not path.is_file():
        return []
    return [Release(**entry) for entry in json.loads(path.read_text(encoding="utf-8"))]


def parse_findings(text: str) -> tuple[str, ...]:
    """Return the finding IDs `inv image-scan` printed, in the order it printed them."""
    return tuple(dict.fromkeys(m.group("id") for m in _FINDING_LINE.finditer(text)))


def read_scans(path: Path) -> dict[str, Scan]:
    """Read the JSON lines the workflow writes for each `inv image-scan` it ran, by image.

    Each line names the image, whether the pull succeeded, the scan's exit code, the digest
    reference the pull resolved, and the log the pull and the scan wrote.
    """
    if not path.is_file():
        return {}
    scans = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        log = str(entry.get("log", ""))
        log_path = Path(log)
        text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
        findings = parse_findings(text)
        if not entry.get("pulled"):
            status = UNAVAILABLE
        elif entry.get("exit_code") == 0:
            status = CLEAN
        elif findings:
            status = FINDINGS
        else:
            status = ERROR
        image = str(entry["image"])
        scans[image] = Scan(image, status, findings, str(entry.get("resolved", "")), log)
    return scans


def cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def markdown(releases: list[Release], scans: Mapping[str, Scan]) -> str:
    lines = ["## Upstream releases", ""]
    if not releases:
        lines += ["The release check wrote no result, so no component was compared.", ""]
        return "\n".join(lines)
    failed = [r for r in releases if r.error]
    newer = [r for r in releases if r.newer]
    lines += [
        f"{len(releases)} component(s) checked, {len(newer)} with a newer release, "
        f"{len(failed)} whose release could not be read.",
        "",
        "| Component | Repository | Pinned | Newest | Mode | Action |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for release in releases:
        modes = ", ".join(
            f"{label} {value}"
            for label, value in (("binary", release.mode), ("image", release.image_mode))
            if value
        )
        newest = f"[{release.newest}]({release.url})" if release.url else release.newest
        lines.append(
            "| "
            + " | ".join(
                cell(value)
                for value in (
                    release.component,
                    release.repository,
                    release.pinned,
                    newest or "unknown",
                    modes or "pinned",
                    release.advice(),
                )
            )
            + " |"
        )
    scanned = [r for r in releases if r.scan_image]
    if scanned:
        lines += [
            "",
            "### Upstream image scans",
            "",
            "Each image the platform builds instead of copying is compared with its newest "
            "upstream image. A clean upstream image can return the component to release mode.",
            "",
            "| Component | Image | Result |",
            "| --- | --- | --- |",
        ]
        for release in scanned:
            scan = scans.get(release.scan_image)
            if scan is None:
                result = "Not scanned, because the scan step did not run."
            elif scan.status == CLEAN and scan.resolved:
                result = (
                    f"{scan.describe()} Set {release.prefix}_IMAGE_MODE=release and "
                    f"{release.image_pin}={scan.resolved}."
                )
            else:
                result = scan.describe()
            lines.append(f"| {cell(release.component)} | `{release.scan_image}` | {cell(result)} |")
    if failed:
        lines += ["", "### Release checks that failed", ""]
        lines += [f"- {r.component} ({r.repository}). {cell(r.error)}" for r in failed]
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    checking = commands.add_parser("check", help="Read the newest release of each component.")
    checking.add_argument("--output", type=Path, required=True, help="Where to write the result.")
    checking.add_argument(
        "--targets", type=Path, default=None, help="Where to write the upstream images to scan."
    )
    checking.add_argument("--versions", type=Path, default=base_versions.VERSIONS_FILE)
    reporting = commands.add_parser("report", help="Render the result and the scans as markdown.")
    reporting.add_argument("--releases", type=Path, required=True)
    reporting.add_argument("--scans", type=Path, default=Path("image-report/scans.jsonl"))
    reporting.add_argument("--output", type=Path, default=None, help="Defaults to stdout.")
    args = parser.parse_args(argv)

    if args.command == "check":
        releases = check(base_versions.load(args.versions), fetch_json)
        write_releases(args.output, releases)
        if args.targets is not None:
            args.targets.parent.mkdir(parents=True, exist_ok=True)
            args.targets.write_text(json.dumps(scan_targets(releases), indent=2) + "\n")
        for release in releases:
            print(f"[upstream-releases] {release.component} {release.pinned}. {release.advice()}")
        return 0

    text = markdown(read_releases(args.releases), read_scans(args.scans))
    if args.output is None:
        sys.stdout.write(text)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
