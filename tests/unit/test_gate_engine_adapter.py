"""Unit tests for the AI-CLI security-compliance reviewer adapter."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from adapters.interfaces.security_scanner import HIGH, LOW, ScanTarget
from services.orchestration.gates.engine_source import (
    CommandResult,
    GateEngineConfig,
    ResolvedEngineSource,
)
from services.orchestration.gates.gate_engine import (
    AiReviewScanner,
    AiTestClassifier,
    GateEngineError,
    map_classifications,
    map_review_findings,
    triage_action,
)


def test_map_review_findings_maps_severity_location_and_rule():
    review = {
        "review_action": "REQUEST_CHANGES",
        "comments": [
            {
                "path": "app/db.py",
                "line": 20,
                "severity": "HIGH",
                "title": "SQL injection",
                "perspective": "security",
            },
            {
                "path": "infra/main.tf",
                "line": 5,
                "severity": "LOW",
                "message": "loose egress",
                "category": "compliance",
            },
        ],
    }

    findings = map_review_findings(review, scanner="ai-security-review")

    assert [f.severity for f in findings] == [HIGH, LOW]
    assert findings[0].location == "app/db.py:20"
    assert findings[0].rule_id == "security"
    assert findings[0].title == "SQL injection"
    assert findings[1].rule_id == "compliance"
    assert all(f.scanner == "ai-security-review" for f in findings)


def test_map_review_findings_reads_description_as_detail():
    review = {
        "review_action": "COMMENT",
        "comments": [
            {
                "path": "app.py",
                "line": 3,
                "side": "RIGHT",
                "perspective": "security",
                "severity": "HIGH",
                "title": "SQL injection vulnerability",
                "description": "User-controlled name is interpolated into the SQL string.",
                "suggestion_kind": "applicable",
            }
        ],
    }
    findings = map_review_findings(review, scanner="ai-security-review")
    assert len(findings) == 1
    assert findings[0].detail == "User-controlled name is interpolated into the SQL string."
    assert findings[0].severity == HIGH
    assert findings[0].title == "SQL injection vulnerability"


def test_map_review_findings_on_approve_is_empty():
    assert map_review_findings({"review_action": "APPROVE", "comments": []}, scanner="x") == []


def _resolved(path: str) -> ResolvedEngineSource:
    return ResolvedEngineSource(path=path, mode="local", version="c" * 40)


def _install_sarif_entrypoint(root: Path) -> None:
    entry = root / "bin" / "scan"
    entry.parent.mkdir(parents=True)
    entry.write_text("#!/usr/bin/env bash\n")
    entry.chmod(0o755)


async def test_sarif_engine_runs_without_a_model_and_parses_findings(tmp_path):
    source_root = tmp_path / "engine"
    source_root.mkdir()
    _install_sarif_entrypoint(source_root)
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    captured: dict[str, Any] = {}

    async def fake_runner(args, cwd, env):
        captured["env"] = dict(env)
        json_out = Path(args[args.index("--json-out") + 1])
        json_out.write_text(
            json.dumps(
                {
                    "version": "2.1.0",
                    "runs": [
                        {
                            "tool": {"driver": {"name": "semgrep"}},
                            "results": [
                                {
                                    "ruleId": "py/sqli",
                                    "level": "error",
                                    "message": {"text": "SQL injection"},
                                    "properties": {"security-severity": "9.1"},
                                }
                            ],
                        }
                    ],
                }
            )
        )
        return CommandResult(returncode=1, stdout="", stderr="")

    scanner = AiReviewScanner(
        name="semgrep-sarif",
        config=GateEngineConfig(
            enabled=True,
            mode="local",
            local_path=str(source_root),
            output_format="sarif",
            review_entrypoint="bin/scan",
        ),
        runner=fake_runner,
        source=_resolved(str(source_root)),
    )

    findings = await scanner.scan(
        ScanTarget(repo="agentic-demo-service", ref="origin/main", repo_root=str(repo_root))
    )

    assert [f.severity for f in findings] == ["critical"]
    assert findings[0].rule_id == "py/sqli"
    assert "ANTHROPIC_BASE_URL" not in captured["env"]
    assert "AI_REVIEW_MODEL" not in captured["env"]
    assert captured["env"]["AI_REVIEW_SCM"] == "github"


def _install_entrypoint(root: Path) -> None:
    entry = root / "engines" / "security-compliance-review" / "harness" / "ai-pr-review"
    entry.parent.mkdir(parents=True)
    entry.write_text("#!/usr/bin/env bash\n")
    entry.chmod(0o755)


async def test_scan_runs_the_entrypoint_and_maps_findings(tmp_path):
    source_root = tmp_path / "engine"
    source_root.mkdir()
    _install_entrypoint(source_root)
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    captured: dict[str, Any] = {}

    async def fake_runner(args, cwd, env):
        captured["args"] = list(args)
        captured["cwd"] = cwd
        captured["env"] = dict(env)
        json_out = Path(args[args.index("--json-out") + 1])
        json_out.write_text(
            json.dumps(
                {
                    "review_action": "REQUEST_CHANGES",
                    "comments": [{"path": "a.py", "line": 1, "severity": "HIGH", "title": "leak"}],
                }
            )
        )
        return CommandResult(returncode=1, stdout="", stderr="")

    scanner = AiReviewScanner(
        name="ai-security-review",
        config=GateEngineConfig(
            enabled=True,
            mode="local",
            local_path=str(source_root),
            review_model="security-review-model",
        ),
        profile="cms-ars",
        litellm_base_url="http://litellm:4000",
        litellm_api_key="sk-gateway",
        runner=fake_runner,
        source=_resolved(str(source_root)),
    )

    findings = await scanner.scan(
        ScanTarget(
            repo="agentic-demo-service",
            ref="headsha",
            repo_root=str(repo_root),
            base_ref="basesha",
        )
    )

    assert [f.severity for f in findings] == [HIGH]
    assert "--against" in captured["args"]
    assert captured["args"][captured["args"].index("--against") + 1] == "basesha"
    assert "--profile" not in captured["args"]
    assert captured["env"]["AI_REVIEW_PROFILE"] == "cms-ars"
    assert captured["cwd"] == str(repo_root)
    assert captured["env"]["ANTHROPIC_BASE_URL"] == "http://litellm:4000"
    assert captured["env"]["OPENAI_BASE_URL"] == "http://litellm:4000"
    assert captured["env"]["ANTHROPIC_API_KEY"] == "sk-gateway"
    assert captured["env"]["OPENAI_API_KEY"] == "sk-gateway"
    assert captured["env"]["AI_REVIEW_PROVIDER"] == "api"
    assert captured["env"]["AI_REVIEW_SCM"] == "github"
    assert not any(p.name.startswith(".ai-review-") for p in repo_root.iterdir())


async def test_scan_fails_closed_without_a_gateway_base_url(tmp_path):
    source_root = tmp_path / "engine"
    source_root.mkdir()
    _install_entrypoint(source_root)
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    calls: list[object] = []

    async def spy_runner(args, cwd, env):
        calls.append(args)
        return CommandResult(returncode=0, stdout="", stderr="")

    scanner = AiReviewScanner(
        name="ai-security-review",
        config=GateEngineConfig(enabled=True, mode="local", local_path=str(source_root)),
        litellm_base_url="",
        runner=spy_runner,
        source=_resolved(str(source_root)),
    )

    with pytest.raises(GateEngineError):
        await scanner.scan(ScanTarget(repo="r", ref="origin/main", repo_root=str(repo_root)))
    assert calls == []


async def test_scan_fails_closed_when_the_engine_errors(tmp_path):
    source_root = tmp_path / "engine"
    source_root.mkdir()
    _install_entrypoint(source_root)
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    async def failing_runner(args, cwd, env):
        return CommandResult(returncode=2, stdout="", stderr="bad config")

    scanner = AiReviewScanner(
        name="ai-security-review",
        config=GateEngineConfig(
            enabled=True,
            mode="local",
            local_path=str(source_root),
            review_model="security-review-model",
        ),
        litellm_base_url="http://litellm:4000",
        litellm_api_key="sk-gateway",
        runner=failing_runner,
        source=_resolved(str(source_root)),
    )

    with pytest.raises(GateEngineError):
        await scanner.scan(ScanTarget(repo="r", ref="origin/main", repo_root=str(repo_root)))


async def test_scan_fails_closed_when_entrypoint_missing(tmp_path):
    source_root = tmp_path / "engine"
    source_root.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    async def unused_runner(args, cwd, env):
        return CommandResult(returncode=0, stdout="", stderr="")

    scanner = AiReviewScanner(
        name="ai-security-review",
        config=GateEngineConfig(enabled=True, mode="local", local_path=str(source_root)),
        runner=unused_runner,
        source=_resolved(str(source_root)),
    )

    with pytest.raises(GateEngineError):
        await scanner.scan(ScanTarget(repo="r", ref="origin/main", repo_root=str(repo_root)))


async def test_scan_fails_closed_when_engine_disabled(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    scanner = AiReviewScanner(
        name="ai-security-review",
        config=GateEngineConfig(enabled=False),
    )

    with pytest.raises(GateEngineError):
        await scanner.scan(ScanTarget(repo="r", ref="origin/main", repo_root=str(repo_root)))


async def test_scan_drives_a_config_driven_entrypoint(tmp_path):
    source_root = tmp_path / "engine"
    entry = source_root / "bin" / "review"
    entry.parent.mkdir(parents=True)
    entry.write_text("#!/usr/bin/env bash\n")
    entry.chmod(0o755)
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    captured: dict[str, Any] = {}

    async def fake_runner(args, cwd, env):
        captured["args"] = list(args)
        json_out = Path(args[args.index("--json-out") + 1])
        json_out.write_text(json.dumps({"review_action": "APPROVE", "comments": []}))
        return CommandResult(returncode=0, stdout="", stderr="")

    scanner = AiReviewScanner(
        name="ai-security-review",
        config=GateEngineConfig(
            enabled=True,
            mode="local",
            local_path=str(source_root),
            review_model="security-review-model",
            review_entrypoint="bin/review",
        ),
        litellm_base_url="http://litellm:4000",
        litellm_api_key="sk-gateway",
        runner=fake_runner,
        source=_resolved(str(source_root)),
    )

    await scanner.scan(ScanTarget(repo="r", ref="origin/main", repo_root=str(repo_root)))

    assert captured["args"][0] == str(entry)


def test_map_classifications_patches_for_application_and_test_bugs():
    payload = {
        "mode": "OBSERVED",
        "summary": "1 app bug, 1 test bug",
        "classifications": [
            {
                "test": "t1",
                "path": "a.py",
                "line": 1,
                "verdict": "APPLICATION_BUG",
                "in_scope": True,
            },
            {"test": "t2", "path": "b.py", "line": 2, "verdict": "TEST_BUG", "in_scope": True},
        ],
    }

    triage = map_classifications(payload)

    assert triage.should_patch is True
    assert triage.flaky_only is False
    assert [c.verdict for c in triage.classifications] == ["APPLICATION_BUG", "TEST_BUG"]


def test_map_classifications_test_bug_alone_still_patches():
    payload = {"classifications": [{"test": "t", "verdict": "TEST_BUG", "in_scope": True}]}

    assert map_classifications(payload).should_patch is True


def test_map_classifications_flaky_only_reruns_without_patch():
    payload = {
        "classifications": [
            {"test": "t1", "verdict": "FLAKY_FAILURE", "in_scope": False},
            {"test": "t2", "verdict": "FLAKY_FAILURE", "in_scope": False},
        ]
    }

    triage = map_classifications(payload)

    assert triage.should_patch is False
    assert triage.flaky_only is True
    assert triage.has_environment_issue is False


def test_map_classifications_environment_issue_is_flagged():
    payload = {
        "classifications": [
            {"test": "t1", "verdict": "ENVIRONMENT_ISSUE", "in_scope": False},
        ]
    }

    triage = map_classifications(payload)

    assert triage.should_patch is False
    assert triage.flaky_only is False
    assert triage.has_environment_issue is True


def test_map_classifications_patchable_dominates_mixed_failures():
    payload = {
        "classifications": [
            {"test": "t1", "verdict": "APPLICATION_BUG", "in_scope": True},
            {"test": "t2", "verdict": "FLAKY_FAILURE", "in_scope": False},
            {"test": "t3", "verdict": "ENVIRONMENT_ISSUE", "in_scope": False},
        ]
    }

    triage = map_classifications(payload)

    assert triage.should_patch is True
    assert triage.flaky_only is False


def test_map_classifications_ignores_out_of_scope_application_bug():
    payload = {
        "classifications": [
            {"test": "t1", "verdict": "APPLICATION_BUG", "in_scope": False},
        ]
    }

    assert map_classifications(payload).should_patch is False


def _install_classifier_entrypoint(root: Path) -> None:
    entry = root / "engines" / "test-classifier" / "harness" / "ai-test-classifier"
    entry.parent.mkdir(parents=True)
    entry.write_text("#!/usr/bin/env bash\n")
    entry.chmod(0o755)


async def test_classify_runs_the_entrypoint_and_maps_verdicts(tmp_path):
    source_root = tmp_path / "engine"
    source_root.mkdir()
    _install_classifier_entrypoint(source_root)
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    async def fake_runner(args, cwd, env):
        json_out = Path(args[args.index("--json-out") + 1])
        json_out.write_text(
            json.dumps(
                {
                    "mode": "OBSERVED",
                    "summary": "1 failed",
                    "classifications": [
                        {"test": "t1", "verdict": "APPLICATION_BUG", "in_scope": True}
                    ],
                }
            )
        )
        return CommandResult(returncode=0, stdout="", stderr="")

    classifier = AiTestClassifier(
        name="test-triage",
        config=GateEngineConfig(
            enabled=True,
            mode="local",
            local_path=str(source_root),
            triage_model="review-model",
        ),
        litellm_base_url="http://litellm:4000",
        litellm_api_key="sk-gateway",
        runner=fake_runner,
        source=_resolved(str(source_root)),
    )

    triage = await classifier.classify(
        ScanTarget(repo="r", ref="origin/main", repo_root=str(repo_root))
    )

    assert triage.mode == "OBSERVED"
    assert triage.should_patch is True
    assert not any(p.name.startswith(".ai-classify-") for p in repo_root.iterdir())


def _triage(**kw):
    from services.orchestration.gates.gate_engine import FailureTriage

    base = dict(
        mode="OBSERVED",
        summary="",
        classifications=[],
        should_patch=False,
        flaky_only=False,
        has_environment_issue=False,
    )
    base.update(kw)
    return FailureTriage(**base)


def test_triage_action_patch_dominates():
    assert triage_action(_triage(should_patch=True, has_environment_issue=True)) == "patch"


def test_triage_action_environment_escalates():
    assert triage_action(_triage(has_environment_issue=True)) == "escalate"


def test_triage_action_flaky_only_reruns():
    assert triage_action(_triage(flaky_only=True)) == "rerun"


def test_triage_action_unclassified_escalates():
    assert triage_action(_triage()) == "escalate"


async def test_scan_uses_the_pod_runner_when_provided(tmp_path):
    source_root = tmp_path / "engine"
    source_root.mkdir()
    _install_entrypoint(source_root)

    class _FakePodRunner:
        def __init__(self):
            self.specs = []

        async def run_engine(self, spec):
            self.specs.append(spec)
            return {
                "review_action": "COMMENT",
                "comments": [
                    {
                        "path": "a.py",
                        "line": 2,
                        "severity": "HIGH",
                        "title": "sqli",
                        "description": "bad",
                    }
                ],
            }

    pod = _FakePodRunner()
    scanner = AiReviewScanner(
        name="ai-security-review",
        config=GateEngineConfig(
            enabled=True,
            mode="local",
            local_path=str(source_root),
            review_model="security-review-model",
        ),
        profile="cms-ars",
        litellm_base_url="http://host.docker.internal:4000",
        litellm_api_key="sk-gateway",
        source=_resolved(str(source_root)),
        pod_runner=pod,
    )

    findings = await scanner.scan(
        ScanTarget(
            repo="agentic-demo-service",
            ref="head",
            repo_root="/checkout",
            base_ref="base",
            execution_id="exec-1",
        )
    )

    assert [f.severity for f in findings] == [HIGH]
    assert findings[0].detail == "bad"
    assert len(pod.specs) == 1
    spec = pod.specs[0]
    assert spec.base_ref == "base"
    assert spec.checkout_dir == "/checkout"
    assert spec.source_path == str(source_root)
    assert spec.env["AI_REVIEW_PROFILE"] == "cms-ars"
    assert spec.env["ANTHROPIC_BASE_URL"] == "http://host.docker.internal:4000"
    assert spec.env["ANTHROPIC_CUSTOM_HEADERS"] == (
        "langfuse_session_id: exec-1\nlangfuse_generation_name: ai-security-review"
    )


async def test_scan_threads_tls_verify_into_the_pod_spec(tmp_path):
    source_root = tmp_path / "engine"
    source_root.mkdir()
    _install_entrypoint(source_root)

    class _CapturingPodRunner:
        def __init__(self):
            self.specs = []

        async def run_engine(self, spec):
            self.specs.append(spec)
            return {"review_action": "COMMENT", "comments": []}

    pod = _CapturingPodRunner()
    scanner = AiReviewScanner(
        name="ai-security-review",
        config=GateEngineConfig(
            enabled=True,
            mode="local",
            local_path=str(source_root),
            review_model="security-review-model",
        ),
        profile="cms-ars",
        litellm_base_url="http://host.docker.internal:4000",
        litellm_api_key="sk-gateway",
        source=_resolved(str(source_root)),
        pod_runner=pod,
        tls_verify=False,
    )

    await scanner.scan(
        ScanTarget(
            repo="agentic-demo-service",
            ref="head",
            repo_root="/checkout",
            base_ref="base",
            execution_id="exec-1",
        )
    )

    assert pod.specs[0].tls_verify is False


async def test_image_acts_as_runtime_override_with_local_source(tmp_path):
    source_root = tmp_path / "engine"
    source_root.mkdir()
    _install_entrypoint(source_root)

    class _CapturingPodRunner:
        def __init__(self):
            self.specs = []

        async def run_engine(self, spec):
            self.specs.append(spec)
            return {"review_action": "COMMENT", "comments": []}

    pod = _CapturingPodRunner()
    scanner = AiReviewScanner(
        name="ai-security-review",
        config=GateEngineConfig(
            enabled=True,
            mode="local",
            local_path=str(source_root),
            image="host.docker.internal:5001/gate-tool:519dd2f",
            review_model="security-review-model",
        ),
        profile="cms-ars",
        litellm_base_url="http://host.docker.internal:4000",
        litellm_api_key="sk-gateway",
        source=_resolved(str(source_root)),
        pod_runner=pod,
    )

    await scanner.scan(
        ScanTarget(
            repo="agentic-demo-service",
            ref="head",
            repo_root="/checkout",
            base_ref="base",
            execution_id="exec-1",
        )
    )

    spec = pod.specs[0]
    # The pre-built image is pulled as the runtime, and the local source is still mounted.
    assert spec.image == "host.docker.internal:5001/gate-tool:519dd2f"
    assert spec.source_path == str(source_root)


async def test_scan_raises_scanner_unavailable_when_pod_engine_fails(tmp_path):
    from adapters.interfaces.security_scanner import ScannerUnavailableError
    from services.orchestration.gates.pod_runner import PodEngineError

    source_root = tmp_path / "engine"
    source_root.mkdir()
    _install_entrypoint(source_root)

    class _FailingPodRunner:
        async def run_engine(self, spec):
            raise PodEngineError("gate 'ai-security-review' engine pod exited 1: gateway down")

    scanner = AiReviewScanner(
        name="ai-security-review",
        config=GateEngineConfig(
            enabled=True,
            mode="local",
            local_path=str(source_root),
            review_model="security-review-model",
        ),
        profile="cms-ars",
        litellm_base_url="http://host.docker.internal:4000",
        litellm_api_key="sk-gateway",
        source=_resolved(str(source_root)),
        pod_runner=_FailingPodRunner(),
    )

    with pytest.raises(ScannerUnavailableError):
        await scanner.scan(
            ScanTarget(
                repo="agentic-demo-service",
                ref="head",
                repo_root="/checkout",
                base_ref="base",
                execution_id="exec-1",
            )
        )


async def test_engine_env_injects_trace_headers_only_with_execution():
    from services.orchestration.gates.gate_engine import _engine_env

    cfg = GateEngineConfig(enabled=True, mode="local", review_model="review-model")
    with_exec = _engine_env(
        config=cfg,
        profile="cms-ars",
        gateway_base_url="http://gw",
        gateway_api_key="k",
        execution_id="exec-9",
        generation_name="ai-security-review",
    )
    assert with_exec["ANTHROPIC_CUSTOM_HEADERS"] == (
        "langfuse_session_id: exec-9\nlangfuse_generation_name: ai-security-review"
    )
    without = _engine_env(
        config=cfg,
        profile="cms-ars",
        gateway_base_url="http://gw",
        gateway_api_key="k",
    )
    assert "ANTHROPIC_CUSTOM_HEADERS" not in without
