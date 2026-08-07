"""In-memory security scanner backend for hermetic tests and local runs."""

from __future__ import annotations

from adapters.interfaces.security_scanner import ScanFinding, ScanTarget


class MockSecurityScanner:
    def __init__(self, name: str = "mock", findings: list[ScanFinding] | None = None) -> None:
        self.name = name
        self._findings = list(findings or [])
        self.calls: list[ScanTarget] = []

    def seed(self, findings: list[ScanFinding]) -> None:
        self._findings = list(findings)

    async def scan(self, target: ScanTarget) -> list[ScanFinding]:
        self.calls.append(target)
        return list(self._findings)
