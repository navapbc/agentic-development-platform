"""Run the repository SAST, secret, misconfiguration, and dependency scans as one gate.

Every file scanner reads the same staged copy of the tree, which holds the tracked files and the
untracked files that git does not ignore. A local run and a CI run therefore scan the same file
set, and ignored files such as a developer's `.env` never reach a report. The gitleaks history
scan reads the commits reachable from HEAD, and pip-audit reads the dependency set that
`uv.lock` pins.

The gate fails on any finding that `security/sast-accepted.yaml` does not accept, and on any
register entry that no longer matches a finding, so the register cannot outlive the code it
describes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import tomllib
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import yaml
from services import process

from scripts.config import Settings

ROOT = Path(__file__).resolve().parents[1]
REGISTER = ROOT / "security" / "sast-accepted.yaml"
DEFAULT_OUTPUT = ROOT / ".sast"
LOCK = ROOT / "uv.lock"
FRESH_RELEASE_WINDOW = timedelta(days=7)

SEMGREP_IMAGE: str = Settings.model_fields["semgrep_image"].default
TRIVY_IMAGE: str = Settings.model_fields["trivy_image"].default
GITLEAKS_IMAGE = (
    "ghcr.io/gitleaks/gitleaks:v8.30.1"
    "@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f"
)
SEMGREP_CONFIGS = (
    "p/default",
    "p/python",
    "p/terraform",
    "p/dockerfile",
    "p/secrets",
    "p/owasp-top-ten",
)
BANDIT_EXCLUDED_DIRS = ("tests",)
TRIVY_CACHE_VOLUME = "agentic-dev-platform-trivy-cache"


@dataclass(frozen=True)
class Finding:
    tool: str
    rule: str
    path: str
    line: int
    match: str
    severity: str
    message: str


@dataclass(frozen=True)
class Accepted:
    tool: str
    rule: str
    path: str
    match: str
    justification: str


@dataclass(frozen=True)
class GateResult:
    unaccepted: list[Finding]
    stale: list[Accepted]

    @property
    def ok(self) -> bool:
        return not self.unaccepted and not self.stale


def normalize(text: str) -> str:
    return " ".join(text.split())


def line_digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(normalize(text).encode()).hexdigest()


def source_line(root: Path, path: str, line: int) -> str:
    if line < 1:
        return ""
    try:
        lines = (root / path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return normalize(lines[line - 1]) if line <= len(lines) else ""


def relative(path: str, root: Path) -> str:
    candidate = Path(path)
    for base in (root, Path("/src")):
        try:
            return candidate.relative_to(base).as_posix()
        except ValueError:
            continue
    return candidate.as_posix().removeprefix("./")


def stage_tree(destination: Path) -> int:
    listed = process.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        timeout=process.SHORT_COMMAND_TIMEOUT_SECONDS,
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    copied = 0
    for raw in listed.stdout.split(b"\0"):
        if not raw:
            continue
        source = ROOT / raw.decode()
        if not source.is_file() or source.is_symlink():
            continue
        target = destination / raw.decode()
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied += 1
    return copied


def parse_semgrep(payload: dict[str, Any], root: Path) -> list[Finding]:
    findings = []
    for result in payload.get("results", []):
        path = relative(result["path"], root)
        line = int(result["start"]["line"])
        findings.append(
            Finding(
                tool="semgrep",
                rule=result["check_id"],
                path=path,
                line=line,
                match=source_line(root, path, line),
                severity=str(result["extra"].get("severity", "")),
                message=normalize(str(result["extra"].get("message", "")))[:300],
            )
        )
    return findings


def parse_bandit(payload: dict[str, Any], root: Path) -> list[Finding]:
    findings = []
    for result in payload.get("results", []):
        path = relative(result["filename"], root)
        line = int(result["line_number"])
        findings.append(
            Finding(
                tool="bandit",
                rule=result["test_id"],
                path=path,
                line=line,
                match=source_line(root, path, line),
                severity=f"{result['issue_severity']}/{result['issue_confidence']}",
                message=normalize(result["issue_text"]),
            )
        )
    return findings


def parse_trivy(payload: dict[str, Any], root: Path) -> list[Finding]:
    findings = []
    for result in payload.get("Results") or []:
        path = relative(result["Target"], root)
        for misconfig in result.get("Misconfigurations") or []:
            if misconfig.get("Status") != "FAIL":
                continue
            cause = misconfig.get("CauseMetadata") or {}
            line = int(cause.get("StartLine") or 0)
            findings.append(
                Finding(
                    tool="trivy",
                    rule=misconfig["ID"],
                    path=path,
                    line=line,
                    match=source_line(root, path, line),
                    severity=misconfig.get("Severity", ""),
                    message=normalize(misconfig.get("Title", "")),
                )
            )
        for secret in result.get("Secrets") or []:
            line = int(secret.get("StartLine") or 0)
            findings.append(
                Finding(
                    tool="trivy",
                    rule=secret["RuleID"],
                    path=path,
                    line=line,
                    match=line_digest(source_line(root, path, line)),
                    severity=secret.get("Severity", ""),
                    message=normalize(secret.get("Title", "")),
                )
            )
    return findings


def parse_gitleaks(
    payload: list[dict[str, Any]] | None, root: Path, *, history: bool
) -> list[Finding]:
    findings = []
    for leak in payload or []:
        path = relative(leak["File"], root)
        line = int(leak.get("StartLine") or 0)
        match = (
            f"{leak['Commit']}:{path}:{leak['RuleID']}:{line}"
            if history
            else line_digest(source_line(root, path, line))
        )
        findings.append(
            Finding(
                tool="gitleaks-history" if history else "gitleaks",
                rule=leak["RuleID"],
                path=path,
                line=line,
                match=match,
                severity="",
                message=normalize(leak.get("Description", "")),
            )
        )
    return findings


def parse_pip_audit(payload: dict[str, Any]) -> list[Finding]:
    findings = []
    for dependency in payload.get("dependencies", []):
        for vuln in dependency.get("vulns", []):
            findings.append(
                Finding(
                    tool="pip-audit",
                    rule=vuln["id"],
                    path=dependency["name"],
                    line=0,
                    match=f"{dependency['name']}=={dependency['version']}",
                    severity="",
                    message="fixed in " + ", ".join(vuln.get("fix_versions") or ["no release"]),
                )
            )
    return findings


def load_register(path: Path = REGISTER) -> list[Accepted]:
    entries = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    accepted = []
    for index, entry in enumerate(entries):
        missing = {"tool", "rule", "path", "match", "justification"} - set(entry)
        if missing:
            raise ValueError(f"register entry {index} is missing {sorted(missing)}")
        if not normalize(str(entry["justification"])):
            raise ValueError(f"register entry {index} has an empty justification")
        accepted.append(
            Accepted(
                tool=str(entry["tool"]),
                rule=str(entry["rule"]),
                path=str(entry["path"]),
                match=normalize(str(entry["match"])),
                justification=normalize(str(entry["justification"])),
            )
        )
    return accepted


def _accepts(entry: Accepted, finding: Finding) -> bool:
    return (
        entry.tool == finding.tool
        and entry.rule == finding.rule
        and entry.path == finding.path
        and entry.match == normalize(finding.match)
    )


def evaluate(findings: Iterable[Finding], register: Sequence[Accepted]) -> GateResult:
    used: set[int] = set()
    unaccepted = []
    for finding in findings:
        matches = [index for index, entry in enumerate(register) if _accepts(entry, finding)]
        if matches:
            used.update(matches)
        else:
            unaccepted.append(finding)
    stale = [entry for index, entry in enumerate(register) if index not in used]
    return GateResult(unaccepted=unaccepted, stale=stale)


def _docker(
    image: str,
    args: Sequence[str],
    mounts: Sequence[str],
    env: Sequence[str] = (),
) -> process.CompletedProcess:
    volumes = [part for mount in mounts for part in ("-v", mount)]
    variables = [part for item in env for part in ("-e", item)]
    return process.run(
        ["docker", "run", "--rm", *volumes, *variables, image, *args],
        timeout=process.LONG_COMMAND_TIMEOUT_SECONDS,
        capture_output=True,
        text=True,
    )


def _json(result: process.CompletedProcess, tool: str, accepted_codes: Iterable[int]) -> Any:
    if result.returncode not in set(accepted_codes):
        raise RuntimeError(f"{tool} exited {result.returncode}: {result.stderr.strip()[-2000:]}")
    return json.loads(result.stdout or "null")


def _git(root: Path, *args: str) -> str:
    return process.run(
        ["git", *args],
        timeout=process.SHORT_COMMAND_TIMEOUT_SECONDS,
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def head_commit(root: Path = ROOT) -> str:
    """Return the checked-out commit, whose ancestry is the history a merge would publish.

    Scanning every ref would also read local-only refs, such as agent checkpoints and stashes,
    that a CI checkout does not have, so local and CI runs would report different findings.
    """
    return _git(root, "rev-parse", "HEAD")


def history_mounts(root: Path = ROOT) -> list[str]:
    """Mount the tree and git's object store at their host paths.

    A linked worktree's `.git` is a file naming the main repository's git directory by its host
    path, so that directory must resolve inside the container too.
    """
    mounts = [f"{root}:{root}:ro"]
    common = Path(_git(root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    if not common.is_relative_to(root):
        mounts.append(f"{common}:{common}:ro")
    return mounts


@dataclass(frozen=True)
class FreshRelease:
    name: str
    version: str
    uploaded: str
    age_days: float
    attestation: str


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def fresh_releases(lock: dict[str, Any], now: datetime) -> list[tuple[str, str, str, str]]:
    """Return the locked registry releases first uploaded within the window.

    Each entry is the name, version, earliest upload time, and the file to query for an
    attestation. The upload times come from `uv.lock`, so the ages are computed from the lock
    under review.
    """
    fresh = []
    for package in lock.get("package", []):
        if not package.get("source", {}).get("registry"):
            continue
        artifacts = [*package.get("wheels", [])]
        if "sdist" in package:
            artifacts.append(package["sdist"])
        stamped = [a for a in artifacts if a.get("upload-time") and a.get("url")]
        if not stamped:
            continue
        first = min(stamped, key=lambda artifact: _parse_time(artifact["upload-time"]))
        if now - _parse_time(first["upload-time"]) < FRESH_RELEASE_WINDOW:
            filename = first["url"].rsplit("/", 1)[-1]
            fresh.append((package["name"], package["version"], first["upload-time"], filename))
    return sorted(fresh)


def pypi_attestation(client: httpx.Client, name: str, version: str, filename: str) -> str:
    """Return the publisher of the release file's PyPI provenance, or why there is none."""
    url = f"https://pypi.org/integrity/{quote(name)}/{quote(version)}/{quote(filename)}/provenance"
    try:
        response = client.get(url, headers={"Accept": "application/vnd.pypi.integrity.v1+json"})
    except httpx.HTTPError as exc:
        return f"unknown ({type(exc).__name__})"
    if response.status_code == 404:
        return "unattested"
    if response.status_code != 200:
        return f"unknown (HTTP {response.status_code})"
    bundles = response.json().get("attestation_bundles") or []
    publisher = bundles[0].get("publisher", {}) if bundles else {}
    repository = publisher.get("repository")
    workflow = publisher.get("workflow")
    if not repository:
        return "attested"
    return f"attested by {repository} {workflow}".rstrip()


def fresh_release_report(
    lock_path: Path = LOCK,
    *,
    now: datetime | None = None,
    attestation: Callable[[str, str, str], str] | None = None,
) -> list[FreshRelease]:
    """List locked releases younger than the window with their provenance, for the reviewer.

    The report never fails the scan. A release leaves it once it is older than the window.
    """
    moment = now or datetime.now(UTC)
    fresh = fresh_releases(tomllib.loads(lock_path.read_text(encoding="utf-8")), moment)
    if not fresh:
        return []
    with httpx.Client(timeout=20.0, follow_redirects=True) as client:
        lookup = attestation or (
            lambda name, version, filename: pypi_attestation(client, name, version, filename)
        )
        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(pool.map(lambda entry: lookup(entry[0], entry[1], entry[3]), fresh))
    return [
        FreshRelease(
            name=name,
            version=version,
            uploaded=uploaded,
            age_days=round((moment - _parse_time(uploaded)).total_seconds() / 86400, 1),
            attestation=status,
        )
        for (name, version, uploaded, _), status in zip(fresh, statuses, strict=True)
    ]


def export_lock(output: Path) -> Path:
    """Write every locked dependency with its hashes, the set the images and CI install."""
    destination = output / "lock-requirements.txt"
    process.run(
        ["uv", "export", "--locked", "--all-extras", "--no-emit-project"]
        + ["--format", "requirements-txt", "--output-file", str(destination)],
        timeout=process.MEDIUM_COMMAND_TIMEOUT_SECONDS,
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    return destination


def run_scanners(stage: Path, output: Path) -> dict[str, list[Finding]]:
    semgrep_args = ["semgrep", "scan", "--metrics=off", "--json", "--quiet"]
    for config in SEMGREP_CONFIGS:
        semgrep_args += ["--config", config]
    raw: dict[str, Any] = {
        "semgrep": _json(
            _docker(SEMGREP_IMAGE, [*semgrep_args, "/src"], [f"{stage}:/src:ro"]),
            "semgrep",
            (0,),
        ),
        "trivy": _json(
            _docker(
                TRIVY_IMAGE,
                ["fs", "--scanners", "misconfig,secret", "--format", "json", "--quiet", "/src"],
                [f"{stage}:/src:ro", f"{TRIVY_CACHE_VOLUME}:/root/.cache"],
            ),
            "trivy",
            (0,),
        ),
        "gitleaks": _json(
            _docker(
                GITLEAKS_IMAGE,
                ["dir", "/src", "--config", "/src/.gitleaks.toml", "--redact"]
                + ["--no-banner", "--exit-code", "0", "--report-format", "json"]
                + ["--report-path", "-"],
                [f"{stage}:/src:ro"],
            ),
            "gitleaks",
            (0,),
        ),
        "gitleaks-history": _json(
            _docker(
                GITLEAKS_IMAGE,
                ["git", str(ROOT), "--config", str(ROOT / ".gitleaks.toml"), "--redact"]
                + ["--no-banner", "--exit-code", "0", "--report-format", "json"]
                + ["--report-path", "-", f"--log-opts=--full-history {head_commit()}"],
                history_mounts(),
                # The mounted repository belongs to the host user, so git inside the container
                # refuses it as a dubious-ownership repository unless it is marked safe.
                [
                    "GIT_CONFIG_COUNT=1",
                    "GIT_CONFIG_KEY_0=safe.directory",
                    f"GIT_CONFIG_VALUE_0={ROOT}",
                ],
            ),
            "gitleaks-history",
            (0,),
        ),
        "bandit": _json(
            process.run(
                [sys.executable, "-m", "bandit", "-r", str(stage), "-f", "json", "-q"]
                + ["-x", ",".join(str(stage / name) for name in BANDIT_EXCLUDED_DIRS)],
                timeout=process.MEDIUM_COMMAND_TIMEOUT_SECONDS,
                capture_output=True,
                text=True,
            ),
            "bandit",
            (0, 1),
        ),
        "pip-audit": _json(
            process.run(
                [sys.executable, "-m", "pip_audit", "--requirement", str(export_lock(output))]
                + ["--require-hashes", "--disable-pip", "--vulnerability-service", "osv"]
                + ["--format", "json"]
                + ["--progress-spinner", "off"],
                timeout=process.MEDIUM_COMMAND_TIMEOUT_SECONDS,
                capture_output=True,
                text=True,
            ),
            "pip-audit",
            (0, 1),
        ),
    }
    for name, payload in raw.items():
        (output / f"{name}.json").write_text(json.dumps(payload, indent=1), encoding="utf-8")
    return {
        "semgrep": parse_semgrep(raw["semgrep"], stage),
        "bandit": parse_bandit(raw["bandit"], stage),
        "trivy": parse_trivy(raw["trivy"], stage),
        "gitleaks": parse_gitleaks(raw["gitleaks"], stage, history=False)
        + parse_gitleaks(raw["gitleaks-history"], ROOT, history=True),
        "pip-audit": parse_pip_audit(raw["pip-audit"]),
    }


def _print_findings(title: str, findings: Sequence[Finding]) -> None:
    print(f"\n{title} ({len(findings)})")
    for finding in findings:
        location = f"{finding.path}:{finding.line}" if finding.line else finding.path
        print(f"  {finding.tool} {finding.rule} {location} {finding.severity}".rstrip())
        print(f"    {finding.message}")
        print(f"    match: {finding.match}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    output: Path = args.output
    output.mkdir(parents=True, exist_ok=True)
    register = load_register()

    with tempfile.TemporaryDirectory(prefix="sast-stage-") as staging:
        stage = Path(staging)
        print(f"[sast] staged {stage_tree(stage)} files")
        by_tool = run_scanners(stage, output)

    findings = [finding for name in by_tool for finding in by_tool[name]]
    result = evaluate(findings, register)
    (output / "findings.json").write_text(
        json.dumps([asdict(finding) for finding in findings], indent=1), encoding="utf-8"
    )
    for name, tool_findings in by_tool.items():
        print(f"[sast] {name}: {len(tool_findings)} findings")
    print(f"[sast] accepted by the register: {len(findings) - len(result.unaccepted)}")
    if result.unaccepted:
        _print_findings("Findings the register does not accept", result.unaccepted)
    if result.stale:
        print(f"\nRegister entries that match no finding ({len(result.stale)})")
        for entry in result.stale:
            print(f"  {entry.tool} {entry.rule} {entry.path} match: {entry.match}")
    fresh = fresh_release_report()
    (output / "fresh-releases.json").write_text(
        json.dumps([asdict(release) for release in fresh], indent=1), encoding="utf-8"
    )
    window = FRESH_RELEASE_WINDOW.days
    print(f"\nLocked releases younger than {window} days, for review ({len(fresh)})")
    for release in fresh:
        print(f"  {release.name} {release.version} {release.age_days}d {release.attestation}")
    print(f"\n[sast] reports written to {output}")
    print("[sast] PASS" if result.ok else "[sast] FAIL")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
