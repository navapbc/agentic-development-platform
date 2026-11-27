"""Hermetic tests for the security scanner adapter, mediator, and scan_change activity."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from adapters.interfaces.security_scanner import (
    CRITICAL,
    DEPENDENCY,
    HIGH,
    INFO,
    LOW,
    MEDIUM,
    STATIC,
    ScanFinding,
    ScanTarget,
    blocking_count,
    is_blocking,
    severity_rank,
)
from adapters.mock.security_scanner import MockSecurityScanner
from adapters.real.security_scanner import (
    OSV_BAKED_DATABASES,
    CommandResult,
    RealOsvScanner,
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


async def test_a_malware_advisory_is_critical_whatever_severity_trivy_reports():
    sample = json.dumps(
        {
            "Results": [
                {
                    "Target": "package-lock.json",
                    "Vulnerabilities": [
                        {
                            "VulnerabilityID": "GHSA-mh6f-8j2x-4483",
                            "PkgName": "flatmap-stream",
                            "InstalledVersion": "0.1.1",
                            "Severity": "LOW",
                            "CweIDs": ["CWE-506"],
                        }
                    ],
                }
            ]
        }
    )

    findings = await RealTrivyScanner(runner=_runner(sample)).scan(_target())

    assert [f.severity for f in findings] == [CRITICAL]


_OSV_SAMPLE = json.dumps(
    {
        "results": [
            {
                "source": {"path": "/tmp/ws/agent-T-1/web/package-lock.json", "type": "lockfile"},
                "packages": [
                    {
                        "package": {"name": "3layerdipstackbndk47", "version": "0.1.4"},
                        "vulnerabilities": [
                            {
                                "id": "MAL-2026-14698",
                                "aliases": ["GHSA-f6vp-8xqg-j5rv"],
                                "summary": "Malicious code in 3layerdipstackbndk47 (npm)",
                            }
                        ],
                        "groups": [{"ids": ["MAL-2026-14698"], "max_severity": ""}],
                    },
                    {
                        "package": {"name": "lodash", "version": "4.17.15"},
                        "vulnerabilities": [
                            {
                                "id": "GHSA-29mw-wpgm-hmr9",
                                "summary": "ReDoS in lodash",
                                "database_specific": {"severity": "MODERATE"},
                            },
                            {"id": "GHSA-p6mc-m468-83gw", "summary": "Prototype pollution"},
                        ],
                        "groups": [
                            {"ids": ["GHSA-29mw-wpgm-hmr9"], "max_severity": "5.3"},
                            {"ids": ["GHSA-p6mc-m468-83gw"], "max_severity": "7.4"},
                        ],
                    },
                ],
            }
        ]
    }
)


async def test_osv_scanner_reports_malware_as_critical_and_buckets_the_others():
    findings = await RealOsvScanner(runner=_runner(_OSV_SAMPLE, returncode=1)).scan(_target())

    assert [(f.rule_id, f.severity, f.location) for f in findings] == [
        ("MAL-2026-14698", CRITICAL, "web/package-lock.json:3layerdipstackbndk47@0.1.4"),
        ("GHSA-29mw-wpgm-hmr9", MEDIUM, "web/package-lock.json:lodash@4.17.15"),
        ("GHSA-p6mc-m468-83gw", HIGH, "web/package-lock.json:lodash@4.17.15"),
    ]
    assert all(f.kind == DEPENDENCY and f.scanner == "osv-scanner" for f in findings)


async def test_osv_scanner_falls_back_to_its_baked_databases_when_the_download_fails():
    calls: list[list[str]] = []

    async def run(args, cwd):
        calls.append(list(args))
        if "--download-offline-databases" in args:
            return CommandResult(returncode=127, stdout="", stderr="could not load db")
        return CommandResult(returncode=1, stdout=_OSV_SAMPLE, stderr="")

    findings = await RealOsvScanner(runner=run).scan(_target())

    assert len(findings) == 3
    assert [c[:2] for c in calls] == [["env", calls[0][1]], ["env", calls[1][1]]]
    assert calls[1][1] == f"OSV_SCANNER_LOCAL_DB_CACHE_DIRECTORY={OSV_BAKED_DATABASES}"
    assert calls[0][1] != calls[1][1]
    assert "--download-offline-databases" not in calls[1]
    assert calls[1][-2:] == ["-r", "/tmp/ws/agent-T-1"]


async def test_osv_scanner_reports_nothing_for_a_checkout_without_packages():
    findings = await RealOsvScanner(runner=_runner("", returncode=128)).scan(_target())

    assert findings == []


async def test_scanner_invocations_stay_on_the_documented_exit_code_contract():
    calls: list[tuple[list[str], str]] = []

    async def record(args, cwd):
        calls.append((list(args), cwd))
        return CommandResult(returncode=0, stdout="{}", stderr="")

    await RealSemgrepScanner(runner=record).scan(_target())
    await RealTrivyScanner(runner=record).scan(_target())

    semgrep_args, semgrep_cwd = calls[0]
    trivy_args, trivy_cwd = calls[1]
    assert semgrep_args == ["semgrep", "scan", "--json", "--quiet", "/tmp/ws/agent-T-1"]
    assert trivy_args == ["trivy", "fs", "--format", "json", "--quiet", "/tmp/ws/agent-T-1"]
    assert semgrep_cwd == trivy_cwd == "/tmp/ws/agent-T-1"
    assert not any(a.startswith("--error") for a in semgrep_args)
    assert not any(a.startswith("--exit-code") for a in trivy_args)


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
    assert persisted[0].source_component == "gate:semgrep"
    assert persisted[0].content_hash.startswith("sha256:")
    assert persisted[0].metadata["gate"] == "semgrep"
    assert persisted[0].metadata["verdict"] == "block"
    assert persisted[0].metadata["finding_count"] == 1


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


async def test_scan_change_reports_the_evidence_id_the_index_stored(monkeypatch):
    from services.orchestration.temporal_activities import security as security_activity

    findings = [ScanFinding("f1", "semgrep", STATIC, HIGH, "shell injection")]
    mediator = SecurityScanMediator(
        backends=(MockSecurityScanner("semgrep", findings),),
        allowed_repos=frozenset({"agentic-demo-service"}),
        threshold=HIGH,
    )
    monkeypatch.setattr(security_activity, "_build_security_mediator", lambda: mediator)

    class _ReplayedSink:
        async def persist(self, record):
            return "art-stored"

    monkeypatch.setattr("services.review.evidence.PostgresEvidenceSink", _ReplayedSink)

    req = security_activity.ScanChangeRequest(
        task_id="T-1",
        execution_id="exec-1",
        repo="agentic-demo-service",
        ref="c" * 40,
        repo_root="/tmp/ws/agent-T-1",
    )
    result = await security_activity.scan_change(req)

    assert result.evidence_id == "art-stored"


async def test_scan_change_writes_one_evidence_record_per_scanner(monkeypatch):
    from services.orchestration.temporal_activities import security as security_activity

    mediator = SecurityScanMediator(
        backends=(
            MockSecurityScanner(
                "semgrep", [ScanFinding("s1", "semgrep", STATIC, HIGH, "sql injection")]
            ),
            MockSecurityScanner(
                "trivy", [ScanFinding("t1", "trivy", DEPENDENCY, LOW, "minor cve")]
            ),
        ),
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

    assert {record.source_component for record in persisted} == {"gate:semgrep", "gate:trivy"}
    verdicts = {record.metadata["gate"]: record.metadata["verdict"] for record in persisted}
    assert verdicts == {"semgrep": "block", "trivy": "pass"}
    assert result.scanner_verdicts == {"semgrep": "block", "trivy": "pass"}


async def _run_scan_change(monkeypatch, tmp_path, scope: str):
    from services.orchestration.temporal_activities import security as security_activity

    (tmp_path / "untouched.py").write_text("legacy = 1\n")
    (tmp_path / "changed.py").write_text("new = 2\n")

    async def fake_git(args, cwd):
        assert cwd == str(tmp_path)
        if args[1] == "merge-base":
            assert args == ["git", "merge-base", "base1", "HEAD"]
            return 0, "mb1\n", ""
        if args[1] == "diff":
            assert args == ["git", "diff", "--name-only", "-z", "mb1"]
            return 0, "changed.py\0", ""
        assert args == ["git", "ls-files", "--others", "--exclude-standard", "-z"]
        return 0, "", ""

    monkeypatch.setattr("services.orchestration.gates.scan_scope._run_git", fake_git)
    findings = [ScanFinding("f1", "semgrep", STATIC, HIGH, "old debt", location="untouched.py:1")]
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
    monkeypatch.setattr("scripts.config.get_settings", lambda: SimpleNamespace(scan_scope=scope))
    req = security_activity.ScanChangeRequest(
        task_id="T-1",
        execution_id="exec-1",
        repo="agentic-demo-service",
        ref="head1",
        repo_root=str(tmp_path),
        base_ref="base1",
    )
    return await security_activity.scan_change(req), persisted


async def test_scan_change_does_not_block_on_a_finding_the_change_did_not_touch(
    monkeypatch, tmp_path
):
    result, persisted = await _run_scan_change(monkeypatch, tmp_path, "changed")

    assert result.scanner_verdicts == {"semgrep": "pass"}
    assert result.blocking_count == 0
    assert len(result.findings) == 1
    assert persisted[0].metadata["finding_count"] == 1


async def test_repository_scope_restores_blocking_on_an_untouched_file(monkeypatch, tmp_path):
    result, _ = await _run_scan_change(monkeypatch, tmp_path, "repository")

    assert result.scanner_verdicts == {"semgrep": "block"}
    assert result.blocking_count == 1


async def test_an_unrecognised_scan_scope_is_rejected(monkeypatch, tmp_path):
    from services.integration.credentials import ConfigError

    with pytest.raises(ConfigError, match="scan_scope"):
        await _run_scan_change(monkeypatch, tmp_path, "everything")


async def test_mediator_fails_closed_when_a_backend_errors():
    from adapters.interfaces.security_scanner import CRITICAL

    class _BoomScanner:
        name = "ai-security-review"

        async def scan(self, target):
            raise RuntimeError("engine pod build failed")

    class _CleanScanner:
        name = "trivy"

        async def scan(self, target):
            return []

    mediator = SecurityScanMediator(
        backends=(_BoomScanner(), _CleanScanner()),
        allowed_repos=frozenset({"agentic-demo-service"}),
        threshold="high",
    )
    report = await mediator.scan(_target())

    err = [f for f in report.findings if f.scanner == "ai-security-review"]
    assert len(err) == 1
    assert err[0].severity == CRITICAL
    assert err[0].rule_id == "gate-backend-error"
    assert "engine pod build failed" in err[0].detail
    assert report.blocking_count == 1
    assert "trivy" in report.scanners


async def test_mediator_propagates_scanner_unavailable_for_retry():
    from adapters.interfaces.security_scanner import ScannerUnavailableError

    class _UnavailableScanner:
        name = "ai-security-review"

        async def scan(self, target):
            raise ScannerUnavailableError("engine pod exited 1: gateway unreachable")

    mediator = SecurityScanMediator(
        backends=(_UnavailableScanner(),),
        allowed_repos=frozenset({"agentic-demo-service"}),
        threshold="high",
    )

    with pytest.raises(ScannerUnavailableError):
        await mediator.scan(_target())


async def test_a_budget_stop_in_a_gate_engine_blocks_every_scanner_gate(monkeypatch):
    from services.integration.litellm_admin import (
        TENANT_MODEL_BUDGET_EXCEEDED,
        ModelBudgetExceededError,
    )
    from services.orchestration.temporal_activities import security as security_activity

    class _RefusedEngine:
        name = "ai-security-review"

        async def scan(self, target):
            raise ModelBudgetExceededError(TENANT_MODEL_BUDGET_EXCEEDED)

    mediator = SecurityScanMediator(
        backends=(MockSecurityScanner("semgrep", []), _RefusedEngine()),
        allowed_repos=frozenset({"agentic-demo-service"}),
        threshold=HIGH,
    )
    monkeypatch.setattr(security_activity, "_build_security_mediator", lambda: mediator)

    result = await security_activity.scan_change(
        security_activity.ScanChangeRequest(
            task_id="T-1",
            execution_id="exec-1",
            repo="agentic-demo-service",
            ref="c" * 40,
            repo_root="/tmp/ws/agent-T-1",
        )
    )

    assert result.budget_stop == TENANT_MODEL_BUDGET_EXCEEDED
    assert result.scanner_verdicts == {"semgrep": "block", "ai-security-review": "block"}
    assert result.blocking_count == 2
    assert {finding.rule_id for finding in result.findings} == {"gate-backend-error"}
