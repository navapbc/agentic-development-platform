"""Vendor-neutral contract for the security scans the platform mediates."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

CRITICAL = "critical"
HIGH = "high"
MEDIUM = "medium"
LOW = "low"
INFO = "info"
UNKNOWN = "unknown"

STATIC = "static"
DEPENDENCY = "dependency"

DEFAULT_BLOCKING_THRESHOLD = HIGH


class ScannerUnavailableError(Exception):
    """A scanner backend could not run because of an infrastructure failure.

    This is distinct from a scanner that ran and reported findings, and from a backend that is
    misconfigured or broken. It signals a transient condition, for example a build-and-run pod that
    failed to build or whose model gateway was unreachable, so the caller may retry the scan before
    failing the gate closed.
    """

_SEVERITY_RANK = {
    CRITICAL: 5,
    HIGH: 4,
    MEDIUM: 3,
    LOW: 2,
    INFO: 1,
    UNKNOWN: 0,
}


@dataclass(frozen=True)
class ScanTarget:
    repo: str
    ref: str
    repo_root: str
    base_ref: str = ""
    execution_id: str = ""


@dataclass(frozen=True)
class ScanFinding:
    finding_id: str
    scanner: str
    kind: str
    severity: str
    title: str
    detail: str = ""
    location: str = ""
    rule_id: str = ""


@dataclass(frozen=True)
class ScanReport:
    repo: str
    ref: str
    scanners: tuple[str, ...]
    findings: tuple[ScanFinding, ...]
    blocking_count: int
    threshold: str


def severity_rank(severity: str) -> int:
    return _SEVERITY_RANK.get(severity.strip().lower(), 0)


def is_blocking(severity: str, *, threshold: str = DEFAULT_BLOCKING_THRESHOLD) -> bool:
    return severity_rank(severity) >= severity_rank(threshold)


def blocking_count(
    findings: Iterable[ScanFinding], *, threshold: str = DEFAULT_BLOCKING_THRESHOLD
) -> int:
    return sum(1 for f in findings if is_blocking(f.severity, threshold=threshold))


class SecurityScanner(Protocol):
    name: str

    async def scan(self, target: ScanTarget) -> list[ScanFinding]: ...
