"""Hermetic tests for the security scanner adapter, mediator, and scan_change activity."""

from __future__ import annotations

import json

import pytest
from adapters.interfaces.security_scanner import (
    CRITICAL,
    DEPENDENCY,
    HIGH,
    INFO,
    LOW,
    STATIC,
    ScanFinding,
    ScanTarget,
    blocking_count,
    is_blocking,
    severity_rank,
)
from adapters.mock.security_scanner import MockSecurityScanner
from adapters.real.security_scanner import (
    CommandResult,
    RealSemgrepScanner,
    RealTrivyScanner,
    ScannerError,
)
from services.integration.security.mediator import RepoNotAllowedError, SecurityScanMediator
from services.review.evidence import EvidenceRecord

_SEMGREP_SAMPLE = json.dumps(
    {
        "results": [
            {
                "check_id": "python.lang.security.audit.dangerous-subprocess-use",
                "path": "app/run.py",
                "start": {"line": 12, "col": 5},
                "extra": {
                    "message": "Detected subprocess with shell=True",
                    "severity": "ERROR",
                    "metadata": {"shortlink": "https://sg.run/xyz"},
                },
            },
            {
                "check_id": "python.lang.best-practice.minor",
                "path": "app/util.py",
                "start": {"line": 3},
                "extra": {"message": "minor style note", "severity": "INFO"},
            },
        ],
        "errors": [],
    }
)

_TRIVY_SAMPLE = json.dumps(
    {
        "Results": [
            {
                "Target": "requirements.txt",
                "Class": "lang-pkgs",
                "Type": "pip",
                "Vulnerabilities": [
                    {
                        "VulnerabilityID": "CVE-2023-0001",
                        "PkgName": "requests",
                        "InstalledVersion": "2.0.0",
                        "FixedVersion": "2.31.0",
                        "Severity": "CRITICAL",
                        "Title": "RCE in requests",
                        "Description": "remote code execution",
                    },
                    {
                        "VulnerabilityID": "CVE-2023-0002",
                        "PkgName": "urllib3",
                        "InstalledVersion": "1.0",
                        "Severity": "LOW",
                        "Title": "minor issue",
                    },
                ],
            }
        ]
    }
)


def _runner(stdout: str, returncode: int = 0):
    async def run(args, cwd):
        return CommandResult(returncode=returncode, stdout=stdout, stderr="boom")

    return run


def _target() -> ScanTarget:
    return ScanTarget(repo="agentic-demo-service", ref="c" * 40, repo_root="/tmp/ws/agent-T-1")


def test_severity_ranking_and_blocking_threshold():
    assert severity_rank(CRITICAL) > severity_rank(HIGH) > severity_rank(LOW)
    assert is_blocking(CRITICAL, threshold=HIGH)
    assert is_blocking(HIGH, threshold=HIGH)
    assert not is_blocking(LOW, threshold=HIGH)


def test_blocking_count_counts_only_at_or_above_threshold():
    findings = [
        ScanFinding("1", "s", STATIC, CRITICAL, "a"),
        ScanFinding("2", "s", STATIC, HIGH, "b"),
        ScanFinding("3", "s", STATIC, LOW, "c"),
    ]
    assert blocking_count(findings, threshold=HIGH) == 2


async def test_semgrep_backend_normalizes_static_findings():
    scanner = RealSemgrepScanner(runner=_runner(_SEMGREP_SAMPLE))
    findings = await scanner.scan(_target())

    assert [f.severity for f in findings] == [HIGH, INFO]
    assert all(f.kind == STATIC for f in findings)
    assert findings[0].scanner == "semgrep"
    assert findings[0].location == "app/run.py:12"
    assert findings[0].rule_id == "python.lang.security.audit.dangerous-subprocess-use"


async def test_trivy_backend_normalizes_dependency_findings():
    scanner = RealTrivyScanner(runner=_runner(_TRIVY_SAMPLE))
    findings = await scanner.scan(_target())

    assert [f.severity for f in findings] == [CRITICAL, LOW]
    assert all(f.kind == DEPENDENCY for f in findings)
    assert findings[0].scanner == "trivy"
    assert findings[0].rule_id == "CVE-2023-0001"
    assert findings[0].location == "requirements.txt:requests@2.0.0"


async def test_backend_raises_on_unexpected_exit_code():
    scanner = RealTrivyScanner(runner=_runner("", returncode=2))
    with pytest.raises(ScannerError):
        await scanner.scan(_target())


async def test_mediator_aggregates_backends_and_counts_blocking():
    semgrep = RealSemgrepScanner(runner=_runner(_SEMGREP_SAMPLE))
    trivy = RealTrivyScanner(runner=_runner(_TRIVY_SAMPLE))
    mediator = SecurityScanMediator(
        backends=(semgrep, trivy),
        allowed_repos=frozenset({"agentic-demo-service"}),
        threshold=HIGH,
    )

    report = await mediator.scan(_target())

    assert report.scanners == ("semgrep", "trivy")
    assert len(report.findings) == 4
    assert report.blocking_count == 2
    assert report.threshold == HIGH


async def test_mediator_refuses_non_allowlisted_repository():
    mediator = SecurityScanMediator(
        backends=(MockSecurityScanner(),),
        allowed_repos=frozenset({"agentic-demo-service"}),
        threshold=HIGH,
    )
    with pytest.raises(RepoNotAllowedError):
        await mediator.scan(ScanTarget(repo="not-allowed", ref="x", repo_root="/tmp/x"))


async def test_scan_change_activity_records_evidence_and_surfaces_blocking(monkeypatch):
    from services.orchestration.temporal_activities import security as security_activity

    findings = [
        ScanFinding("f1", "semgrep", STATIC, HIGH, "shell injection", location="a.py:1"),
        ScanFinding("f2", "trivy", DEPENDENCY, LOW, "minor cve", rule_id="CVE-1"),
    ]
    mediator = SecurityScanMediator(
        backends=(MockSecurityScanner("semgrep", findings),),
        allowed_repos=frozenset({"agentic-demo-service"}),
        threshold=HIGH,
    )
    monkeypatch.setattr(security_activity, "_build_security_mediator", lambda: mediator)

    persisted: list[EvidenceRecord] = []

    class _FakeSink:
        async def persist(self, record):
            persisted.append(record)
            return record.artifact_id

    monkeypatch.setattr("services.review.evidence.PostgresEvidenceSink", _FakeSink)

    req = security_activity.ScanChangeRequest(
        task_id="T-1",
        execution_id="exec-1",
        repo="agentic-demo-service",
        ref="c" * 40,
        repo_root="/tmp/ws/agent-T-1",
    )
    result = await security_activity.scan_change(req)

    assert result.scanners == ["semgrep"]
    assert result.blocking_count == 1
    assert result.severity_counts == {HIGH: 1, LOW: 1}
    assert len(result.findings) == 2
    assert result.content_hash.startswith("sha256:")
    assert len(persisted) == 1
    assert persisted[0].content_hash == result.content_hash
    assert persisted[0].metadata["blocking_count"] == 1
    assert persisted[0].metadata["scanners"] == ["semgrep"]


async def test_scan_change_content_hash_is_deterministic(monkeypatch):
    from services.orchestration.temporal_activities import security as security_activity

    findings = [ScanFinding("f1", "semgrep", STATIC, HIGH, "shell injection")]
    mediator = SecurityScanMediator(
        backends=(MockSecurityScanner("semgrep", findings),),
        allowed_repos=frozenset({"agentic-demo-service"}),
        threshold=HIGH,
    )
    monkeypatch.setattr(security_activity, "_build_security_mediator", lambda: mediator)

    class _FakeSink:
        async def persist(self, record):
            return record.artifact_id

    monkeypatch.setattr("services.review.evidence.PostgresEvidenceSink", _FakeSink)

    req = security_activity.ScanChangeRequest(
        task_id="T-1",
        execution_id="exec-1",
        repo="agentic-demo-service",
        ref="c" * 40,
        repo_root="/tmp/ws/agent-T-1",
    )
    first = await security_activity.scan_change(req)
    second = await security_activity.scan_change(req)

    assert first.content_hash == second.content_hash
