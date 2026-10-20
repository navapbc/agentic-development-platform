"""Real security scanner backends: Semgrep for SAST and Trivy for dependency scans.

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
    def __init__(self, scanner: str, returncode: int, stderr: str) -> None:
        super().__init__(f"{scanner} exited {returncode}: {stderr.strip()[:400]}")
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
            severity = _TRIVY_SEVERITY.get(str(vuln.get("Severity") or "").upper(), UNKNOWN)
            vuln_id = str(vuln.get("VulnerabilityID") or "")
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
    name = "trivy"
    _ok_codes = frozenset({0})

    def __init__(self, *, binary: str = "trivy", runner: CommandRunner | None = None) -> None:
        self._binary = binary
        self._runner = runner or run_subprocess

    @classmethod
    def from_settings(
        cls, settings: Settings | None = None, *, runner: CommandRunner | None = None
    ) -> RealTrivyScanner:
        resolved = settings or get_settings()
        return cls(binary=resolved.trivy_binary, runner=runner)

    async def scan(self, target: ScanTarget) -> list[ScanFinding]:
        args = [self._binary, "fs", "--format", "json", "--quiet", target.repo_root]
        result = await self._runner(args, target.repo_root)
        if result.returncode not in self._ok_codes:
            raise ScannerError(self.name, result.returncode, result.stderr)
        return _trivy_findings(json.loads(result.stdout or "{}"))
