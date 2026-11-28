"""Real security scanner backends: Semgrep for SAST, and Trivy and OSV-Scanner for dependencies.

Each backend shells out to its tool over an injectable command seam and normalizes the
tool's JSON into the shared ScanFinding vocabulary. The concrete tool never reaches the
workflow; callers depend only on the SecurityScanner contract.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from adapters.interfaces.security_scanner import (
    CRITICAL,
    DEPENDENCY,
    HIGH,
    INFO,
    LOW,
    MEDIUM,
    STATIC,
    UNKNOWN,
    ScanFinding,
    ScanTarget,
    is_malicious,
)
from scripts.config import Settings, get_settings
from services import process


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


CommandRunner = Callable[[Sequence[str], str], Awaitable[CommandResult]]

_SEMGREP_SEVERITY = {"ERROR": HIGH, "WARNING": MEDIUM, "INFO": INFO}
_TRIVY_SEVERITY = {
    "CRITICAL": CRITICAL,
    "HIGH": HIGH,
    "MEDIUM": MEDIUM,
    "LOW": LOW,
    "UNKNOWN": UNKNOWN,
}


class ScannerError(Exception):
    """A scanner that exited with a failure code, carrying the end of its stderr.

    A scan in a pod writes the image pull before the tool runs, so the tool's own error is last.
    """

    def __init__(self, scanner: str, returncode: int, stderr: str) -> None:
        super().__init__(f"{scanner} exited {returncode}: {stderr.strip()[-400:]}")
        self.scanner = scanner
        self.returncode = returncode


async def run_subprocess(args: Sequence[str], cwd: str) -> CommandResult:
    try:
        result = await process.run_async(
            args, timeout=process.LONG_COMMAND_TIMEOUT_SECONDS, cwd=cwd or None
        )
    except FileNotFoundError as exc:
        raise ScannerError(
            args[0] if args else "scanner",
            127,
            f"{args[0] if args else 'scanner'} is not installed on this worker. "
            "Install it or set scanner_execution to 'pod' to run it from its own image.",
        ) from exc
    return CommandResult(
        returncode=result.returncode,
        stdout=result.stdout.decode("utf-8", "replace"),
        stderr=result.stderr.decode("utf-8", "replace"),
    )


def _semgrep_findings(payload: dict) -> list[ScanFinding]:
    findings: list[ScanFinding] = []
    for index, result in enumerate(payload.get("results") or []):
        extra = result.get("extra") or {}
        severity = _SEMGREP_SEVERITY.get(str(extra.get("severity") or "").upper(), UNKNOWN)
        path = str(result.get("path") or "")
        line = (result.get("start") or {}).get("line")
        location = f"{path}:{line}" if line else path
        check_id = str(result.get("check_id") or index)
        findings.append(
            ScanFinding(
                finding_id=f"semgrep:{check_id}:{location}",
                scanner="semgrep",
                kind=STATIC,
                severity=severity,
                title=str(extra.get("message") or check_id),
                detail=str((extra.get("metadata") or {}).get("shortlink") or ""),
                location=location,
                rule_id=check_id,
            )
        )
    return findings


def _trivy_findings(payload: dict) -> list[ScanFinding]:
    findings: list[ScanFinding] = []
    for result in payload.get("Results") or []:
        target = str(result.get("Target") or "")
        for vuln in result.get("Vulnerabilities") or []:
            vuln_id = str(vuln.get("VulnerabilityID") or "")
            severity = (
                CRITICAL
                if is_malicious([vuln_id], vuln.get("CweIDs") or ())
                else _TRIVY_SEVERITY.get(str(vuln.get("Severity") or "").upper(), UNKNOWN)
            )
            package = str(vuln.get("PkgName") or "")
            version = str(vuln.get("InstalledVersion") or "")
            findings.append(
                ScanFinding(
                    finding_id=f"trivy:{vuln_id}:{package}@{version}",
                    scanner="trivy",
                    kind=DEPENDENCY,
                    severity=severity,
                    title=str(vuln.get("Title") or vuln_id),
                    detail=str(vuln.get("Description") or ""),
                    location=f"{target}:{package}@{version}",
                    rule_id=vuln_id,
                )
            )
    return findings


class RealSemgrepScanner:
    name = "semgrep"
    _ok_codes = frozenset({0, 1})

    def __init__(self, *, binary: str = "semgrep", runner: CommandRunner | None = None) -> None:
        self._binary = binary
        self._runner = runner or run_subprocess

    @classmethod
    def from_settings(
        cls, settings: Settings | None = None, *, runner: CommandRunner | None = None
    ) -> RealSemgrepScanner:
        resolved = settings or get_settings()
        return cls(binary=resolved.semgrep_binary, runner=runner)

    async def scan(self, target: ScanTarget) -> list[ScanFinding]:
        args = [self._binary, "scan", "--json", "--quiet", target.repo_root]
        result = await self._runner(args, target.repo_root)
        if result.returncode not in self._ok_codes:
            raise ScannerError(self.name, result.returncode, result.stderr)
        return _semgrep_findings(json.loads(result.stdout or "{}"))


class RealTrivyScanner:
    """Trivy over the checkout, from the database in `cache_dir` when one is given.

    With a `cache_dir`, the scan reads the database already there and makes no network request.
    Without one, Trivy downloads its database first.
    """

    name = "trivy"
    _ok_codes = frozenset({0})

    def __init__(
        self, *, binary: str = "trivy", runner: CommandRunner | None = None, cache_dir: str = ""
    ) -> None:
        self._binary = binary
        self._runner = runner or run_subprocess
        self._cache_dir = cache_dir

    @classmethod
    def from_settings(
        cls,
        settings: Settings | None = None,
        *,
        runner: CommandRunner | None = None,
        cache_dir: str = "",
    ) -> RealTrivyScanner:
        resolved = settings or get_settings()
        return cls(binary=resolved.trivy_binary, runner=runner, cache_dir=cache_dir)

    async def scan(self, target: ScanTarget) -> list[ScanFinding]:
        offline = (
            [
                "--cache-dir",
                self._cache_dir,
                "--skip-db-update",
                "--skip-java-db-update",
                "--offline-scan",
            ]
            if self._cache_dir
            else []
        )
        args = [self._binary, "fs", "--format", "json", "--quiet", *offline, target.repo_root]
        result = await self._runner(args, target.repo_root)
        if result.returncode not in self._ok_codes:
            raise ScannerError(self.name, result.returncode, result.stderr)
        return _trivy_findings(json.loads(result.stdout or "{}"))


# The osv-scanner image carries its npm, PyPI, Go, and Maven databases in the first directory,
# for a scan whose pod cannot download fresh ones, and downloads into the second.
OSV_BAKED_DATABASES = "/opt/osv-scanner/db"
_OSV_FRESH_DATABASES = "/opt/osv-scanner/fresh"
_OSV_SEVERITY = {**_TRIVY_SEVERITY, "MODERATE": MEDIUM}
_OSV_FINDINGS = 1
_OSV_NO_PACKAGES = 128


def _cvss_severity(score: str) -> str:
    try:
        value = float(score)
    except ValueError:
        return UNKNOWN
    for floor, severity in ((9.0, CRITICAL), (7.0, HIGH), (4.0, MEDIUM), (0.1, LOW)):
        if value >= floor:
            return severity
    return UNKNOWN


def _osv_findings(payload: dict, repo_root: str) -> list[ScanFinding]:
    """One finding per advisory group of each package, located like a trivy finding.

    A group joins the aliases of one advisory, and its severity is the advisory's GitHub rating,
    or else the group's highest CVSS score. A malicious-package advisory carries neither.
    """
    root = f"{repo_root.rstrip('/')}/"
    findings: list[ScanFinding] = []
    for result in payload.get("results") or []:
        path = str((result.get("source") or {}).get("path") or "").removeprefix(root)
        for entry in result.get("packages") or []:
            package = entry.get("package") or {}
            name, version = str(package.get("name") or ""), str(package.get("version") or "")
            advisories = {str(v.get("id")): v for v in entry.get("vulnerabilities") or []}
            for group in entry.get("groups") or []:
                ids = [str(i) for i in group.get("ids") or []]
                advisory = next((advisories[i] for i in ids if i in advisories), {})
                specific = advisory.get("database_specific") or {}
                aliases = [*ids, *(advisory.get("aliases") or [])]
                if is_malicious(aliases, specific.get("cwe_ids") or ()):
                    severity = CRITICAL
                else:
                    severity = _OSV_SEVERITY.get(
                        str(specific.get("severity") or "").upper()
                    ) or _cvss_severity(str(group.get("max_severity") or ""))
                advisory_id = ids[0] if ids else ""
                findings.append(
                    ScanFinding(
                        finding_id=f"osv-scanner:{advisory_id}:{name}@{version}",
                        scanner="osv-scanner",
                        kind=DEPENDENCY,
                        severity=severity,
                        title=str(advisory.get("summary") or advisory_id),
                        detail=str(advisory.get("details") or ""),
                        location=f"{path}:{name}@{version}",
                        rule_id=advisory_id,
                    )
                )
    return findings


class RealOsvScanner:
    """OSV-Scanner over the checkout's lockfiles and manifests, from its offline databases.

    Each scan downloads the current databases first. When the download fails, as it does
    without egress to the OSV bucket, the scan runs again on the databases the image carries.
    """

    name = "osv-scanner"
    _ok_codes = frozenset({0, _OSV_FINDINGS})

    def __init__(self, *, binary: str = "osv-scanner", runner: CommandRunner | None = None) -> None:
        self._binary = binary
        self._runner = runner or run_subprocess

    @classmethod
    def from_settings(
        cls, settings: Settings | None = None, *, runner: CommandRunner | None = None
    ) -> RealOsvScanner:
        return cls(runner=runner)

    def _args(self, databases: str, *, download: bool, repo_root: str) -> list[str]:
        return [
            "env",
            f"OSV_SCANNER_LOCAL_DB_CACHE_DIRECTORY={databases}",
            self._binary,
            "scan",
            "source",
            "--offline-vulnerabilities",
            *(["--download-offline-databases"] if download else []),
            "--format",
            "json",
            "-r",
            repo_root,
        ]

    async def scan(self, target: ScanTarget) -> list[ScanFinding]:
        root = target.repo_root
        result = await self._runner(
            self._args(_OSV_FRESH_DATABASES, download=True, repo_root=root), root
        )
        if result.returncode not in self._ok_codes | {_OSV_NO_PACKAGES}:
            result = await self._runner(
                self._args(OSV_BAKED_DATABASES, download=False, repo_root=root), root
            )
        if result.returncode == _OSV_NO_PACKAGES:
            return []
        if result.returncode not in self._ok_codes:
            raise ScannerError(self.name, result.returncode, result.stderr)
        return _osv_findings(json.loads(result.stdout or "{}"), root)
