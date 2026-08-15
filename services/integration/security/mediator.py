"""Platform security scan mediator: allowlist-guarded facade aggregating scanner backends."""

from __future__ import annotations

from dataclasses import dataclass

from adapters.interfaces.security_scanner import (
    DEFAULT_BLOCKING_THRESHOLD,
    ScanFinding,
    ScanReport,
    ScanTarget,
    SecurityScanner,
    blocking_count,
)
from scripts.config import Settings, get_settings
from services.integration.github.credentials import allowed_repos


class RepoNotAllowedError(Exception):
    def __init__(self, repo: str) -> None:
        super().__init__(f"repository not allowlisted: {repo}")
        self.repo = repo


@dataclass
class SecurityScanMediator:
    backends: tuple[SecurityScanner, ...]
    allowed_repos: frozenset[str]
    threshold: str = DEFAULT_BLOCKING_THRESHOLD

    @classmethod
    def from_settings(
        cls,
        backends: tuple[SecurityScanner, ...],
        settings: Settings | None = None,
    ) -> SecurityScanMediator:
        resolved = settings or get_settings()
        return cls(
            backends=backends,
            allowed_repos=frozenset(allowed_repos(resolved)),
            threshold=resolved.security_blocking_threshold,
        )

    def _guard(self, repo: str) -> None:
        if repo not in self.allowed_repos:
            raise RepoNotAllowedError(repo)

    async def scan(self, target: ScanTarget) -> ScanReport:
        self._guard(target.repo)
        findings: list[ScanFinding] = []
        for backend in self.backends:
            findings.extend(await backend.scan(target))
        return ScanReport(
            repo=target.repo,
            ref=target.ref,
            scanners=tuple(backend.name for backend in self.backends),
            findings=tuple(findings),
            blocking_count=blocking_count(findings, threshold=self.threshold),
            threshold=self.threshold,
        )
