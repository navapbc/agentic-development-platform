"""Temporal activity that scans a change and records normalized findings as evidence."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field

from adapters.interfaces.security_scanner import ScanReport, ScanTarget
from temporalio import activity


@dataclass
class ScanChangeRequest:
    task_id: str
    execution_id: str
    repo: str
    ref: str
    repo_root: str
    base_ref: str = ""


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
    scanner_verdicts: dict[str, str] = field(default_factory=dict)


def _build_security_mediator():
    from services.integration.security.mediator import SecurityScanMediator
    from services.orchestration.gates.config import load_gates_config
    from services.orchestration.gates.registry import scanner_backends_for_config

    config = load_gates_config()
    return SecurityScanMediator.from_settings(scanner_backends_for_config(config))


def _content_hash(report: ScanReport) -> str:
    ordered = sorted((asdict(f) for f in report.findings), key=lambda f: f["finding_id"])
    payload = json.dumps(
        {"repo": report.repo, "ref": report.ref, "findings": ordered}, sort_keys=True
    )
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def _scanner_content_hash(report: ScanReport, scanner: str, findings: list) -> str:
    ordered = sorted((asdict(f) for f in findings), key=lambda f: f["finding_id"])
    payload = json.dumps(
        {"repo": report.repo, "ref": report.ref, "scanner": scanner, "findings": ordered},
        sort_keys=True,
    )
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


@activity.defn
async def scan_change(req: ScanChangeRequest) -> ScanChangeResult:
    from adapters.interfaces.security_scanner import is_blocking

    from services.review.evidence import EvidenceRecord, build_evidence_sink

    mediator = _build_security_mediator()
    report = await mediator.scan(
        ScanTarget(
            repo=req.repo,
            ref=req.ref,
            repo_root=req.repo_root,
            base_ref=req.base_ref,
            execution_id=req.execution_id,
        )
    )
    content_hash = _content_hash(report)
    severity_counts = dict(Counter(f.severity for f in report.findings))

    scanner_verdicts = {name: "pass" for name in report.scanners}
    for finding in report.findings:
        if finding.scanner in scanner_verdicts and is_blocking(
            finding.severity, threshold=report.threshold
        ):
            scanner_verdicts[finding.scanner] = "block"

    sink = build_evidence_sink()
    evidence_ids: dict[str, str] = {}
    for scanner in report.scanners:
        scoped = [f for f in report.findings if f.scanner == scanner]
        scanner_hash = _scanner_content_hash(report, scanner, scoped)
        artifact_id = uuid.uuid4().hex
        evidence_ids[scanner] = artifact_id
        await sink.persist(
            EvidenceRecord(
                artifact_id=artifact_id,
                execution_id=req.execution_id,
                content_hash=scanner_hash,
                source_component=f"gate:{scanner}",
                classification="final",
                location=None,
                metadata={
                    "gate": scanner,
                    "kind": "scanner",
                    "repo": report.repo,
                    "ref": report.ref,
                    "threshold": report.threshold,
                    "verdict": scanner_verdicts[scanner],
                    "finding_count": len(scoped),
                    "findings": [asdict(f) for f in scoped],
                },
            )
        )

    return ScanChangeResult(
        scanners=list(report.scanners),
        findings=[ScanFindingRecord(**asdict(f)) for f in report.findings],
        blocking_count=report.blocking_count,
        threshold=report.threshold,
        content_hash=content_hash,
        evidence_id=next(iter(evidence_ids.values()), ""),
        severity_counts=severity_counts,
        scanner_verdicts=scanner_verdicts,
    )
