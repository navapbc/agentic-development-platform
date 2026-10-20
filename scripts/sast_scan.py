"""Run the repository SAST, secret, misconfiguration, and dependency scans as one gate.

Every file scanner reads the same staged copy of the tree, which holds the tracked files and the
untracked files that git does not ignore. A local run and a CI run therefore scan the same file
set, and ignored files such as a developer's `.env` never reach a report. The gitleaks history
scan reads the commit history of the repository itself, and pip-audit reads the dependency
set that `uv.lock` pins.

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
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml
from services import process

from scripts.config import Settings

ROOT = Path(__file__).resolve().parents[1]
REGISTER = ROOT / "security" / "sast-accepted.yaml"
DEFAULT_OUTPUT = ROOT / ".sast"

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
                ["git", "/repo", "--config", "/repo/.gitleaks.toml", "--redact"]
                + ["--no-banner", "--exit-code", "0", "--report-format", "json"]
                + ["--report-path", "-"],
                [f"{ROOT}:/repo:ro"],
                # The mounted repository belongs to the host user, so git inside the container
                # refuses it as a dubious-ownership repository unless it is marked safe.
                [
                    "GIT_CONFIG_COUNT=1",
                    "GIT_CONFIG_KEY_0=safe.directory",
                    "GIT_CONFIG_VALUE_0=/repo",
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
                + ["--require-hashes", "--disable-pip", "--format", "json"]
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
    print(f"\n[sast] reports written to {output}")
    print("[sast] PASS" if result.ok else "[sast] FAIL")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
