"""End-to-end test that two configured gate bundles drive promotion from config alone.

It exercises the plugin gate, the engine gate, parallel and fail-fast stages, the fail-closed
missing plugin, and the human-pause gate. The gate engine and Snyk plugin have separate
environment-gated smoke tests under tests/integration.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from adapters.interfaces.ci_host import SUCCESS
from adapters.interfaces.policy_engine import PolicyGate
from adapters.interfaces.security_scanner import HIGH, ScanFinding, ScanTarget
from services.orchestration.gates.builtin import scanner_gate
from services.orchestration.gates.config import parse_gates_config
from services.orchestration.gates.contract import BLOCK, CANCELLED, GateContext, GateNode
from services.orchestration.gates.engine_source import (
    CommandResult,
    GateEngineConfig,
    ResolvedEngineSource,
)
from services.orchestration.gates.gate_engine import AiReviewScanner
from services.orchestration.gates.plugins import (
    PluginError,
    registry_for_config,
    scanner_backends_for_config,
)
from services.orchestration.gates.runner import run_gate_stage
from services.orchestration.suspension import ResumeOn, SuspendReason
from services.orchestration.temporal_workflows.draft_pull_request import (
    REVIEW_BLOCKED,
    REVIEW_PASSED,
    DraftPullRequestWorkflow,
)
from temporalio.testing import WorkflowEnvironment
from tests.unit.test_draft_pull_request_workflow import (
    _await_human_park_stage,
    _ci_input,
    _make_activities,
    _start_ci_worker,
)
from tests.unit.test_policy_engine import _client, _compliant

pytestmark = pytest.mark.asyncio


class _FakeDist:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeEntryPoint:
    def __init__(self, name: str, distribution: str, loaded) -> None:
        self.name = name
        self.dist = _FakeDist(distribution)
        self._loaded = loaded

    def load(self):
        return self._loaded


class _SnykScanner:
    name = "snyk"

    async def scan(self, target: ScanTarget) -> list[ScanFinding]:
        return [
            ScanFinding(
                finding_id="snyk-1",
                scanner="snyk",
                kind="dependency",
                severity="high",
                title="vulnerable dependency",
                rule_id="SNYK-1",
            )
        ]


def _snyk_discover():
    return lambda group: [_FakeEntryPoint("snyk", "snyk-gate", _SnykScanner)]


def _empty_discover():
    return lambda group: []


_ENGINE = GateEngineConfig(
    name="ai-security-review", enabled=True, mode="local", local_path="/tmp/engine"
)


def _engine_registry():
    from services.orchestration.gates.builtin import engine_gate
    from services.orchestration.gates.registry import builtin_registry

    registry = builtin_registry()
    registry.register("ai-security-review", lambda: engine_gate(_ENGINE))
    return registry


def _government_raw() -> dict[str, Any]:
    return {
        "profile": "government",
        "gates": [
            {
                "name": "snyk",
                "enabled": True,
                "required": True,
                "plugin": {"distribution": "snyk-gate", "entry_point": "snyk"},
            },
            {"name": "ai-security-review", "enabled": True, "required": True},
        ],
    }


def _private_raw() -> dict[str, Any]:
    return {"profile": "private"}


def _context() -> GateContext:
    return GateContext(
        task_id="AGENTDEV-1", execution_id="exec-exit", repo="agentic-demo-service", ref="a" * 40
    )


def test_two_bundles_select_different_gate_sets():
    government = parse_gates_config(_government_raw(), registry=_engine_registry())
    private = parse_gates_config(_private_raw())

    gov_required = {spec.name for spec in government.enabled_gates() if spec.required}
    assert {"code-review", "security-review", "acceptance", "semgrep", "trivy", "snyk",
            "ai-security-review"} <= gov_required

    private_names = {spec.name for spec in private.enabled_gates()}
    assert "snyk" not in private_names
    semgrep = private.gate("semgrep")
    assert semgrep is not None and semgrep.required is False


def test_government_bundle_resolves_the_plugin_and_engine_gate():
    config = parse_gates_config(_government_raw(), registry=_engine_registry())
    registry = registry_for_config(config, discover=_snyk_discover(), engines=(_ENGINE,))

    assert registry.has("snyk")
    assert registry.resolve("snyk").kind == "scanner"
    assert registry.resolve("ai-security-review").kind == "scanner"
    assert registry.resolve("code-review").kind == "review"


async def test_a_required_snyk_high_finding_denies_promotion():
    gate = scanner_gate("snyk", _SnykScanner)
    result = await gate.evaluate(_context())
    assert result.verdict == BLOCK

    decision = await _client().evaluate(
        _compliant(
            gates=[
                PolicyGate(name="code-review", required=True, verdict="pass"),
                PolicyGate(name="snyk", required=True, verdict=result.verdict),
            ],
            required_gates=["code-review", "snyk"],
        )
    )

    assert decision.allow is False
    assert "gate_failed:snyk" in decision.reasons


async def test_a_disabled_gate_is_neither_run_nor_required():
    raw = _government_raw()
    raw["gates"].append({"name": "snyk", "enabled": False})
    config = parse_gates_config(raw, registry=_engine_registry())

    enabled = {spec.name for spec in config.enabled_gates()}
    assert "snyk" not in enabled

    decision = await _client().evaluate(
        _compliant(
            gates=[PolicyGate(name="code-review", required=True, verdict="pass")],
            required_gates=[spec.name for spec in config.enabled_gates() if spec.required],
        )
    )
    assert all("snyk" not in reason for reason in decision.reasons)


async def test_the_engine_gate_maps_findings_into_the_scanner_family(tmp_path):
    source_root = tmp_path / "engine"
    entry = source_root / "engines" / "security-compliance-review" / "harness" / "ai-pr-review"
    entry.parent.mkdir(parents=True)
    entry.write_text("#!/usr/bin/env bash\n")
    entry.chmod(0o755)
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    async def stub_engine(args, cwd, env):
        json_out = Path(args[args.index("--json-out") + 1])
        json_out.write_text(
            json.dumps(
                {
                    "review_action": "REQUEST_CHANGES",
                    "comments": [
                        {
                            "path": "auth.py",
                            "line": 3,
                            "severity": "HIGH",
                            "title": "hardcoded secret",
                        }
                    ],
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
        litellm_base_url="http://litellm:4000",
        litellm_api_key="sk-gateway",
        runner=stub_engine,
        source=ResolvedEngineSource(path=str(source_root), mode="local", version="c" * 40),
    )
    gate = scanner_gate("ai-security-review", lambda: scanner)

    result = await gate.evaluate(
        GateContext(
            task_id="AGENTDEV-1",
            execution_id="exec-exit",
            repo="agentic-demo-service",
            ref="a" * 40,
            repo_root=str(repo_root),
        )
    )

    assert result.verdict == BLOCK
    assert result.findings[0].severity == HIGH


async def test_enabled_scanners_run_in_parallel():
    active = 0
    peak = 0

    async def execute(spec, context):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return await scanner_gate(spec.name, _pass_scanner(spec.name)).evaluate(context)

    def _pass_scanner(name):
        class _Clean:
            async def scan(self, target):
                return []

        return _Clean

    results = await run_gate_stage(
        [GateNode("semgrep"), GateNode("trivy"), GateNode("snyk")], _context(), execute=execute
    )

    assert peak == 3
    assert {result.name for result in results} == {"semgrep", "trivy", "snyk"}


async def test_fail_fast_cancels_siblings_on_a_required_block():
    async def execute(spec, context):
        if spec.name == "snyk":
            return await scanner_gate("snyk", _SnykScanner).evaluate(context)
        await asyncio.sleep(10)
        return await scanner_gate(spec.name, _SnykScanner).evaluate(context)

    results = await run_gate_stage(
        [GateNode("snyk", required=True), GateNode("trivy", required=True)],
        _context(),
        execute=execute,
        fail_fast=True,
    )

    verdicts = {result.name: result.verdict for result in results}
    assert verdicts["snyk"] == BLOCK
    assert verdicts["trivy"] == CANCELLED


def test_a_missing_required_plugin_fails_closed():
    config = parse_gates_config(_government_raw(), registry=_engine_registry())
    with pytest.raises(PluginError):
        registry_for_config(config, discover=_empty_discover(), engines=(_ENGINE,))


def test_an_advisory_missing_plugin_drops_from_the_backends():
    raw = {
        "profile": "private",
        "gates": [
            {
                "name": "snyk",
                "enabled": True,
                "required": False,
                "plugin": {"distribution": "snyk-gate", "entry_point": "snyk"},
            }
        ],
    }
    config = parse_gates_config(raw)
    backends = scanner_backends_for_config(config, discover=_empty_discover(), engines=())
    assert all(getattr(backend, "name", "") != "snyk" for backend in backends)


async def test_a_pause_for_human_gate_parks_and_resumes():
    alerts: list = []
    activities = _make_activities(
        None,
        1,
        [],
        [],
        [],
        ci_result=SUCCESS,
        security_verdict="block",
        review_on_block={"security-review": "pause_for_human"},
        alerts=alerts,
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"exit-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(run_reviews=True),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
            state = await _await_human_park_stage(handle, "review")
            assert state["reason"] == SuspendReason.AWAITING_HUMAN.value
            assert state["resume_on"] == ResumeOn.HUMAN_APPROVAL.value
            await handle.signal(
                "submit_gate_decision", args=["security-review", True, "security-lead"]
            )
            approved = await handle.result()

            reject_handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(run_reviews=True),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
            await _await_human_park_stage(reject_handle, "review")
            await reject_handle.signal(
                "submit_gate_decision", args=["security-review", False, "security-lead"]
            )
            rejected = await reject_handle.result()

    assert approved.status == REVIEW_PASSED
    assert rejected.status == REVIEW_BLOCKED
