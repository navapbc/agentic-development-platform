"""Platform security scan mediator: allowlist-guarded facade aggregating scanner backends."""

from __future__ import annotations

from dataclasses import dataclass

from adapters.interfaces.security_scanner import (
    CRITICAL,
    DEFAULT_BLOCKING_THRESHOLD,
    ScanFinding,
    ScannerUnavailableError,
    ScanReport,
    ScanTarget,
    SecurityScanner,
    blocking_count,
)
from scripts.config import Settings, get_settings
from services.integration.github.credentials import allowed_repos


def _backend_error_finding(scanner: str, exc: Exception) -> ScanFinding:
    """A scanner whose backend fails yields one critical finding so the gate fails closed.

    A scan backend that is absent or broken, for example an external tool image that fails to
    build or run, must block promotion rather than crash the scan stage or silently drop, and it
    must not take the other scanners down with it. The critical finding drives the scanner's
    verdict to block through the same path a real finding does.
    """
    reason = f"{type(exc).__name__}: {exc}".strip()
    return ScanFinding(
        finding_id=f"{scanner}-backend-error",
        scanner=scanner,
        kind="error",
        severity=CRITICAL,
        title=f"gate {scanner!r} backend failed",
        detail=reason[:800],
        rule_id="gate-backend-error",
    )


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
            try:
                findings.extend(await backend.scan(target))
            except ScannerUnavailableError:
                # Infrastructure failure, not a finding. Propagate so the activity retries the
                # scan before the gate fails closed on exhausted retries.
                raise
            except Exception as exc:  # noqa: BLE001
                # Any other backend failure fails the gate closed as a finding.
                findings.append(_backend_error_finding(backend.name, exc))
        return ScanReport(
            repo=target.repo,
            ref=target.ref,
            scanners=tuple(backend.name for backend in self.backends),
            findings=tuple(findings),
            blocking_count=blocking_count(findings, threshold=self.threshold),
            threshold=self.threshold,
        )
