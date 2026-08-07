"""Temporal activity that scans a change and records normalized findings as evidence."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field

from adapters.interfaces.security_scanner import ScanReport, ScanTarget
from temporalio import activity

_SOURCE_COMPONENT = "security-scanner"


@dataclass
class ScanChangeRequest:
    task_id: str
    execution_id: str
    repo: str
    ref: str
    repo_root: str


@dataclass
class ScanFindingRecord:
    finding_id: str
    scanner: str
    kind: str
    severity: str
    title: str
    detail: str = ""
    location: str = ""
    rule_id: str = ""


@dataclass
class ScanChangeResult:
    scanners: list[str]
    findings: list[ScanFindingRecord]
    blocking_count: int
    threshold: str
    content_hash: str
    evidence_id: str
    severity_counts: dict[str, int] = field(default_factory=dict)


def _build_security_mediator():
    from adapters.real.security_scanner import RealSemgrepScanner, RealTrivyScanner

    from services.integration.security.mediator import SecurityScanMediator

    return SecurityScanMediator.from_settings(
        (RealSemgrepScanner.from_settings(), RealTrivyScanner.from_settings())
    )


def _content_hash(report: ScanReport) -> str:
    ordered = sorted((asdict(f) for f in report.findings), key=lambda f: f["finding_id"])
    payload = json.dumps(
        {"repo": report.repo, "ref": report.ref, "findings": ordered}, sort_keys=True
    )
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


@activity.defn
async def scan_change(req: ScanChangeRequest) -> ScanChangeResult:
    from services.review.evidence import EvidenceRecord, PostgresEvidenceSink

    mediator = _build_security_mediator()
    report = await mediator.scan(ScanTarget(repo=req.repo, ref=req.ref, repo_root=req.repo_root))
    content_hash = _content_hash(report)
    severity_counts = dict(Counter(f.severity for f in report.findings))

    evidence_id = uuid.uuid4().hex
    record = EvidenceRecord(
        artifact_id=evidence_id,
        execution_id=req.execution_id,
        content_hash=content_hash,
        source_component=_SOURCE_COMPONENT,
        classification="final",
        location=None,
        metadata={
            "repo": report.repo,
            "ref": report.ref,
            "scanners": list(report.scanners),
            "threshold": report.threshold,
            "blocking_count": report.blocking_count,
            "finding_count": len(report.findings),
            "severity_counts": severity_counts,
            "findings": [asdict(f) for f in report.findings],
        },
    )
    await PostgresEvidenceSink().persist(record)

    return ScanChangeResult(
        scanners=list(report.scanners),
        findings=[ScanFindingRecord(**asdict(f)) for f in report.findings],
        blocking_count=report.blocking_count,
        threshold=report.threshold,
        content_hash=content_hash,
        evidence_id=evidence_id,
        severity_counts=severity_counts,
    )
