"""Re-check the platform's and every tenant's image exceptions against the newest scans.

The review flags an entry that has expired, an entry that expires within the warning window, and
an entry whose finding a completed scan no longer reports, either the scan of the image's rebuild
or the scan of the component's newest upstream image. `targets` lists the images the daily
`publish-images` run scans with `inv image-scan` for the entries, and `report` renders the review
as markdown.

The files come from the source TENANT_CONFIG_BUCKET selects, platform/image_exceptions.yaml and
tenants/<tenant>/image_exceptions.yaml in the bucket, or config/<environment>/ without it.
`report --prune` removes the platform entries that expired or whose finding the rebuild or the
excepted image no longer reports, through a conditional write, and lists each removal and the
file's new version. Tenant entries are only reported, since each tenant publishes its own file.
A missing or unreadable file is a problem, which `report` prints as a GitHub error annotation,
writes to the --warnings file, and signals with exit code 1.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from scripts.upstream_releases import Release, Scan, cell, read_releases, read_scans

DEFAULT_WARN_DAYS = 14
EXPIRED = "expired"
EXPIRING = "expiring"
CLEARED = "cleared"
REBUILD = "rebuild"
EXCEPTED = "excepted image"
UPSTREAM = "newest upstream image"
PLATFORM = "platform"


@dataclass(frozen=True)
class Target:
    label: str
    image: str


@dataclass(frozen=True)
class Review:
    exception: Any
    days_left: int
    flags: tuple[str, ...]
    evidence: tuple[tuple[Target, Scan | None], ...]

    def describe(self) -> str:
        expires = self.exception.expires.isoformat()
        sentences = []
        if EXPIRED in self.flags:
            sentences.append(f"Expired on {expires}, so the scan gate refuses the finding again.")
        elif EXPIRING in self.flags:
            sentences.append(f"Expires in {self.days_left} day(s).")
        finding = self.exception.finding
        clearing = [target for target, scan in self.evidence if _clears(finding, scan)]
        if clearing:
            named = " and the ".join(f"{t.label} `{t.image}`" for t in clearing)
            verb = "reports" if len(clearing) == 1 else "report"
            sentences.append(f"The {named} no longer {verb} it. Remove the entry.")
        elif any(scan is not None and scan.completed for _, scan in self.evidence):
            sentences.append("Every completed scan still reports it.")
        else:
            sentences.append("No scan completed, so the finding was not re-checked.")
        return " ".join(sentences)

    def scans(self) -> str:
        states = []
        for target, scan in self.evidence:
            if scan is None or not scan.completed:
                state = "not scanned"
            elif _clears(self.exception.finding, scan):
                state = "does not report it"
            else:
                state = "reports it"
            states.append(f"{target.label} {state}")
        text = ", ".join(states)
        return f"{text[:1].upper()}{text[1:]}." if text else ""


def _clears(finding: str, scan: Scan | None) -> bool:
    return scan is not None and scan.completed and finding not in scan.findings


@dataclass(frozen=True)
class Loaded:
    """One exceptions file as the review read it."""

    owner: str
    location: str
    exceptions: tuple[Any, ...] = ()
    version: str = ""
    problem: str = ""
    file: Any = None

    @property
    def platform(self) -> bool:
        return self.owner == PLATFORM


@dataclass(frozen=True)
class Pruned:
    location: str
    version: str
    removed: tuple[tuple[Any, str], ...]


def _settings() -> Any:
    from scripts.config import get_settings

    return get_settings()


def load_platform(settings: Any, path_override: str = "") -> Loaded:
    from services.integration.image_exceptions import LocalFile, platform_file, read_platform

    file = LocalFile(Path(path_override)) if path_override else platform_file(settings)
    try:
        snapshot, exceptions = read_platform(settings, path_override=path_override)
    except Exception as exc:  # noqa: BLE001
        return Loaded(PLATFORM, file.location, problem=str(exc), file=file)
    return Loaded(PLATFORM, file.location, exceptions, snapshot.version, file=file)


def load_tenants(settings: Any) -> list[Loaded]:
    """Read every tenant's file. Scope is checked when a bundle is validated, not here."""
    from services.integration.image_exceptions import parse_text, tenant_files
    from services.integration.image_policy import TENANT_SCOPE

    try:
        files = tenant_files(settings)
    except Exception as exc:  # noqa: BLE001
        return [Loaded("tenants", "the tenant files", problem=f"cannot be listed: {exc}")]
    loaded = []
    for tenant, file in files:
        owner = f"tenant {tenant}"
        try:
            snapshot = file.read()
            exceptions = parse_text(snapshot.text, TENANT_SCOPE, snapshot.location)
        except Exception as exc:  # noqa: BLE001
            loaded.append(Loaded(owner, file.location, problem=str(exc), file=file))
            continue
        loaded.append(Loaded(owner, file.location, exceptions, snapshot.version, file=file))
    return loaded


def removal_reason(entry: Review) -> str:
    """Return why the review removes a platform entry, or an empty string to keep it."""
    if EXPIRED in entry.flags:
        return f"expired on {entry.exception.expires.isoformat()}"
    for target, scan in entry.evidence:
        if target.label in (REBUILD, EXCEPTED) and _clears(entry.exception.finding, scan):
            return f"the {target.label} `{target.image}` no longer reports it"
    return ""


def prune(file: Any, reviews: Sequence[Review]) -> Pruned:
    """Remove the platform entries `removal_reason` names, keeping any entry written since."""
    from services.integration.image_exceptions import update
    from services.integration.image_policy import PLATFORM_SCOPE

    reasons = {entry.exception: reason for entry in reviews if (reason := removal_reason(entry))}
    if not reasons:
        return Pruned(file.location, "", ())
    result = update(
        file, lambda current: tuple(e for e in current if e not in reasons), scope=PLATFORM_SCOPE
    )
    removed = tuple((e, reasons[e]) for e in result.before if e not in result.after)
    return Pruned(result.location, result.version if result.written else "", removed)


def read_manifest(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _name(image: str) -> str:
    return image.partition("@")[0]


def _upstream_name(release: Release) -> str:
    from services.integration.image_policy import parse_reference

    return parse_reference(release.upstream_image).name if release.upstream_image else ""


def _component(name: str, releases: Iterable[Release]) -> Release | None:
    registry = name.partition("/")[0]
    for release in releases:
        if not release.scan_image:
            continue
        if name == _upstream_name(release):
            return release
        if ".dkr.ecr." in registry and name.rsplit("/", 1)[-1] == release.component:
            return release
    return None


def targets_for(
    exception: Any, manifest: Sequence[Mapping[str, Any]], releases: Sequence[Release]
) -> list[Target]:
    """Return the images whose newest scan decides whether the exception still applies."""
    name = _name(exception.image)
    rebuilt = next((e for e in manifest if e.get("repository") == name), None)
    found = [
        Target(REBUILD, str(rebuilt["reference"])) if rebuilt else Target(EXCEPTED, exception.image)
    ]
    release = _component(name, releases)
    if release is not None:
        found.append(Target(UPSTREAM, release.scan_image))
    return found


def review(
    exceptions: Iterable[Any],
    today: date,
    warn_days: int,
    manifest: Sequence[Mapping[str, Any]],
    releases: Sequence[Release],
    scans: Mapping[str, Scan],
) -> list[Review]:
    reviews = []
    for exception in exceptions:
        days_left = (exception.expires - today).days
        flags = []
        if days_left < 0:
            flags.append(EXPIRED)
        elif days_left <= warn_days:
            flags.append(EXPIRING)
        evidence = tuple(
            (target, scans.get(target.image))
            for target in targets_for(exception, manifest, releases)
        )
        if any(_clears(exception.finding, scan) for _, scan in evidence):
            flags.append(CLEARED)
        reviews.append(Review(exception, days_left, tuple(flags), evidence))
    return reviews


def section(
    loaded: Loaded,
    reviews: Sequence[Review],
    today: date,
    warn_days: int,
    pruned: Pruned | None = None,
) -> str:
    """Render one file's review as a markdown section."""
    title = "Platform exceptions" if loaded.platform else f"{loaded.owner.capitalize()} exceptions"
    lines = [f"### {title}", ""]
    if loaded.problem:
        return "\n".join(
            [*lines, f"`{loaded.location}` could not be reviewed. {cell(loaded.problem)}", ""]
        )
    version = f" at version `{loaded.version}`" if loaded.version else ""
    source = f"The file is `{loaded.location}`{version}."
    if not reviews:
        return "\n".join([*lines, f"{source} It holds no exception.", ""])
    counts = {flag: sum(flag in r.flags for r in reviews) for flag in (EXPIRED, EXPIRING, CLEARED)}
    lines += [
        f"{source} {len(reviews)} exception(s) reviewed on {today.isoformat()}, "
        f"{counts[EXPIRED]} expired, {counts[EXPIRING]} expiring within {warn_days} days, and "
        f"{counts[CLEARED]} whose finding a completed scan no longer reports.",
        "",
        "| Image | Finding | Expires | Review | Scans |",
        "| --- | --- | --- | --- | --- |",
    ]
    for entry in sorted(reviews, key=lambda r: (not r.flags, r.days_left)):
        exception = entry.exception
        lines.append(
            f"| `{exception.image}` | {cell(exception.finding)} | {exception.expires.isoformat()} "
            f"| {cell(entry.describe())} | {cell(entry.scans())} |"
        )
    lines.append("")
    if pruned is not None and pruned.removed:
        lines += [f"Removed from `{pruned.location}`, now at version `{pruned.version}`:", ""]
        lines += [
            f"- {cell(exception.finding)} in `{exception.image}`, {reason}"
            for exception, reason in pruned.removed
        ]
        lines.append("")
    if not loaded.platform:
        lines += ["The review removes no tenant entry. The tenant publishes its own file.", ""]
    return "\n".join(lines)


def warnings(problems: Sequence[str]) -> str:
    """Render the problems as the warning that opens the run report."""
    if not problems:
        return ""
    lines = ["> [!WARNING]", "> The finding exception review is incomplete.", ">"]
    lines += [f"> - {cell(problem)}" for problem in problems]
    return "\n".join([*lines, ""]) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("targets", help="Write the images the review needs scanned.")
    reporting = commands.add_parser("report", help="Render the review as markdown.")
    for command in (listing, reporting):
        command.add_argument(
            "--exceptions",
            default="",
            help="A local platform image_exceptions.yaml to review in place of the configured one.",
        )
        command.add_argument("--manifest", type=Path, default=Path("images-manifest.json"))
        command.add_argument("--releases", type=Path, default=Path("image-report/releases.json"))
    listing.add_argument("--output", type=Path, required=True)
    reporting.add_argument("--scans", type=Path, default=Path("image-report/scans.jsonl"))
    reporting.add_argument("--warn-days", type=int, default=DEFAULT_WARN_DAYS)
    reporting.add_argument("--output", type=Path, default=None, help="Defaults to stdout.")
    reporting.add_argument(
        "--prune", action="store_true", help="Remove the expired and resolved platform entries."
    )
    reporting.add_argument(
        "--warnings", type=Path, default=None, help="Where to write the report's opening warning."
    )
    args = parser.parse_args(argv)

    settings = _settings()
    files = [load_platform(settings, args.exceptions), *load_tenants(settings)]
    manifest, releases = read_manifest(args.manifest), read_releases(args.releases)

    if args.command == "targets":
        for loaded in files:
            if loaded.problem:
                print(f"{loaded.location}: {loaded.problem}", file=sys.stderr)
        found = {
            target.image: {"name": f"{exception.finding} {target.label}", "image": target.image}
            for loaded in files
            for exception in loaded.exceptions
            for target in targets_for(exception, manifest, releases)
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(list(found.values()), indent=2) + "\n", encoding="utf-8")
        return 0

    today = datetime.now(UTC).date()
    scans = read_scans(args.scans)
    problems = [f"`{f.location}` could not be reviewed. {f.problem}" for f in files if f.problem]
    sections = ["## Finding exceptions", ""]
    for loaded in files:
        reviews = review(loaded.exceptions, today, args.warn_days, manifest, releases, scans)
        pruned = None
        if args.prune and loaded.platform and not loaded.problem:
            try:
                pruned = prune(loaded.file, reviews)
            except Exception as exc:  # noqa: BLE001
                problems.append(
                    f"The expired and resolved entries of `{loaded.location}` "
                    f"could not be removed. {exc}"
                )
        sections.append(section(loaded, reviews, today, args.warn_days, pruned))
    text = "\n".join(sections)
    if args.output is None:
        sys.stdout.write(text)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    if args.warnings is not None:
        args.warnings.parent.mkdir(parents=True, exist_ok=True)
        args.warnings.write_text(warnings(problems), encoding="utf-8")
    for problem in problems:
        print(f"::error title=Finding exception review::{_annotation(problem)}", file=sys.stderr)
    return 1 if problems else 0


def _annotation(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


if __name__ == "__main__":
    raise SystemExit(main())
