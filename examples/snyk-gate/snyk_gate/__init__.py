"""Example plugin: Snyk dependency-scan gate."""

from __future__ import annotations

import asyncio
import json
import os

from adapters.interfaces.security_scanner import ScanFinding, ScanTarget

_SEVERITY = {
    "critical": "critical",
    "high": "high",
    "medium": "medium",
    "low": "low",
}


class SnykScanner:
    name = "snyk"

    async def scan(self, target: ScanTarget) -> list[ScanFinding]:
        org = os.environ.get("SNYK_ORG", "")
        args = ["snyk", "test", "--json"]
        if org:
            args.extend(["--org", org])
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=target.repo_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await process.communicate()
        return _parse(stdout.decode("utf-8") or "{}")


def _parse(payload: str) -> list[ScanFinding]:
    document = json.loads(payload)
    findings: list[ScanFinding] = []
    for vuln in document.get("vulnerabilities", []):
        findings.append(
            ScanFinding(
                finding_id=str(vuln.get("id", "")),
                scanner="snyk",
                kind="dependency",
                severity=_SEVERITY.get(str(vuln.get("severity", "")).lower(), "unknown"),
                title=str(vuln.get("title", "")),
                detail=str(vuln.get("packageName", "")),
                location=str(vuln.get("from", [""])[0]),
                rule_id=str(vuln.get("id", "")),
            )
        )
    return findings
