"""Hermetic promotion-policy tests: the OPA client and mediator against the authored policy.

A fake OPA transport reproduces the agentic.promotion decision so the client, the allowlist
guard, and the activity are proven without a running OPA server.
"""

from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest
import services.orchestration.temporal_activities.policy as policy_activity
from adapters.interfaces.policy_engine import PolicyDecision, PolicyGate, PolicyInput
from adapters.real.policy_engine import RealOpaClient
from services.integration.opa.mediator import PromotionPolicyMediator, RepoNotAllowedError
from services.orchestration.temporal_activities.policy import (
    EvaluatePromotionRequest,
    evaluate_promotion,
)

_ALLOWED_PROJECTS = {"AGENTDEV"}
_ALLOWED_REPOS = {"agentic-demo-service", "agentic-demo-greenfield"}
_BUNDLE_VERSION = "0.4.0"


def _policy_decision(inp: dict) -> dict:
    reasons: list[str] = []
    human_review: list[str] = []
    if inp["jira_project"] not in _ALLOWED_PROJECTS:
        reasons.append("jira_project_not_allowlisted")
    if inp["repository"] not in _ALLOWED_REPOS:
        reasons.append("repository_not_allowlisted")
    if inp["ci_status"] != "success":
        reasons.append("ci_not_successful")
    if inp["repair_attempts"] > inp["max_repair_attempts"]:
        reasons.append("repair_attempts_exceeded")
    if not inp["jira_snapshot_current"]:
        reasons.append("jira_snapshot_stale")
    if not inp["pr_authorized"]:
        reasons.append("pr_not_authorized")
    ran = {gate["name"] for gate in inp.get("gates", [])}
    for gate in inp.get("gates", []):
        if gate["required"] and gate["verdict"] != "pass":
            if gate.get("on_block", "fail_closed") == "pause_for_human":
                human_review.append(f"gate_failed:{gate['name']}")
            else:
                reasons.append(f"gate_failed:{gate['name']}")
    for name in inp.get("required_gates", []):
        if name not in ran:
            reasons.append(f"required_gate_missing:{name}")
    allow = len(reasons) == 0 and len(human_review) == 0
    if allow:
        next_action = "proceed"
    elif not reasons and human_review:
        next_action = "await_human"
    else:
        next_action = "block_and_report"
    return {
        "allow": allow,
        "bundle_version": _BUNDLE_VERSION,
        "reasons": reasons,
        "human_review": human_review,
        "next_action": next_action,
    }


def _fake_opa(*, undefined: bool = False) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/data/agentic/promotion/decision"
        if undefined:
            return httpx.Response(200, json={})
        payload = json.loads(request.content)
        return httpx.Response(200, json={"result": _policy_decision(payload["input"])})

    return httpx.MockTransport(handler)


def _client(*, undefined: bool = False) -> RealOpaClient:
    return RealOpaClient(base_url="http://opa:8181", transport=_fake_opa(undefined=undefined))


_COMPLIANT = PolicyInput(
    jira_project="AGENTDEV",
    repository="agentic-demo-service",
    ci_status="success",
    repair_attempts=0,
    max_repair_attempts=2,
    jira_snapshot_current=True,
    pr_authorized=True,
    gates=[
        PolicyGate(name="code-review", required=True, verdict="pass"),
        PolicyGate(name="semgrep", required=True, verdict="pass"),
    ],
    required_gates=["code-review", "semgrep"],
)


def _compliant(**overrides) -> PolicyInput:
    return replace(_COMPLIANT, **overrides)


async def test_compliant_change_is_allowed():
    decision = await _client().evaluate(_compliant())

    assert decision.allow is True
    assert decision.reasons == ()
    assert decision.next_action == "proceed"
    assert decision.bundle_version == _BUNDLE_VERSION


async def test_bundle_version_reads_the_policy_at_start():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/data/agentic/promotion/bundle_version"
        assert request.method == "GET"
        return httpx.Response(200, json={"result": "0.3.0"})

    client = RealOpaClient(base_url="http://opa:8181", transport=httpx.MockTransport(handler))

    assert await client.bundle_version() == "0.3.0"



async def test_missing_ci_success_denies():
    decision = await _client().evaluate(_compliant(ci_status="failure"))

    assert decision.allow is False
    assert decision.next_action == "block_and_report"
    assert "ci_not_successful" in decision.reasons


async def test_a_required_blocking_gate_denies():
    decision = await _client().evaluate(
        _compliant(gates=[PolicyGate(name="code-review", required=True, verdict="block")])
    )

    assert decision.allow is False
    assert "gate_failed:code-review" in decision.reasons


async def test_a_pause_for_human_gate_routes_to_human_review():
    decision = await _client().evaluate(
        _compliant(
            gates=[
                PolicyGate(
                    name="security-review",
                    required=True,
                    verdict="block",
                    on_block="pause_for_human",
                )
            ],
            required_gates=["security-review"],
        )
    )

    assert decision.allow is False
    assert decision.reasons == ()
    assert "gate_failed:security-review" in decision.human_review
    assert decision.next_action == "await_human"


async def test_an_advisory_blocking_gate_is_allowed():
    decision = await _client().evaluate(
        _compliant(
            gates=[
                PolicyGate(name="code-review", required=True, verdict="pass"),
                PolicyGate(name="semgrep", required=False, verdict="block"),
            ],
            required_gates=["code-review"],
        )
    )

    assert decision.allow is True
    assert decision.reasons == ()


async def test_a_required_gate_that_did_not_run_fails_closed():
    decision = await _client().evaluate(
        _compliant(
            gates=[PolicyGate(name="code-review", required=True, verdict="pass")],
            required_gates=["code-review", "trivy"],
        )
    )

    assert decision.allow is False
    assert "required_gate_missing:trivy" in decision.reasons


async def test_stale_jira_snapshot_denies():
    decision = await _client().evaluate(_compliant(jira_snapshot_current=False))

    assert decision.allow is False
    assert "jira_snapshot_stale" in decision.reasons


async def test_undefined_result_fails_closed():
    decision = await _client(undefined=True).evaluate(_compliant())

    assert decision.allow is False
    assert decision.next_action == "block_and_report"


async def test_mediator_refuses_non_allowlisted_repository():
    mediator = PromotionPolicyMediator(
        engine=_client(), allowed_repos=frozenset({"agentic-demo-service"})
    )

    with pytest.raises(RepoNotAllowedError):
        await mediator.evaluate(_compliant(repository="rogue-repo"))


async def test_policy_input_document_uses_policy_field_names():
    document = _compliant().as_document()

    assert document["jira_project"] == "AGENTDEV"
    gates = document["gates"]
    assert isinstance(gates, list)
    assert gates[0] == {
        "name": "code-review",
        "required": True,
        "verdict": "pass",
        "on_block": "fail_closed",
    }
    assert set(document) == {
        "jira_project",
        "repository",
        "ci_status",
        "repair_attempts",
        "max_repair_attempts",
        "jira_snapshot_current",
        "pr_authorized",
        "gates",
        "required_gates",
        "severity_counts",
        "scan_findings",
    }


async def test_evaluate_promotion_activity_surfaces_the_decision(monkeypatch):
    mediator = PromotionPolicyMediator(
        engine=_client(), allowed_repos=frozenset({"agentic-demo-service"})
    )
    monkeypatch.setattr(policy_activity, "_build_policy_mediator", lambda: mediator)

    result = await evaluate_promotion(
        EvaluatePromotionRequest(
            task_id="T-1",
            execution_id="exec-1",
            jira_project="AGENTDEV",
            repository="agentic-demo-service",
            ci_status="success",
            repair_attempts=0,
            max_repair_attempts=2,
            jira_snapshot_current=True,
            pr_authorized=True,
            gates=[PolicyGate(name="semgrep", required=True, verdict="block")],
            required_gates=["semgrep"],
        )
    )

    assert result.allow is False
    assert result.next_action == "block_and_report"
    assert result.reasons == ["gate_failed:semgrep"]
    assert result.bundle_version == _BUNDLE_VERSION


async def test_mock_engine_defaults_to_allow():
    from adapters.mock.policy_engine import MockPolicyEngine

    engine = MockPolicyEngine()
    decision = await engine.evaluate(_compliant())

    assert decision.allow is True
    assert engine.calls[0].repository == "agentic-demo-service"


async def test_mock_engine_returns_seeded_decision():
    from adapters.mock.policy_engine import MockPolicyEngine

    seeded = PolicyDecision(
        allow=False, bundle_version="x", reasons=("gate_failed:code-review",), next_action="block"
    )
    engine = MockPolicyEngine(seeded)

    assert await engine.evaluate(_compliant()) is seeded
