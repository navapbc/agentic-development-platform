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
from adapters.interfaces.policy_engine import PolicyDecision, PolicyInput
from adapters.real.policy_engine import RealOpaClient
from services.integration.opa.mediator import PromotionPolicyMediator, RepoNotAllowedError
from services.orchestration.temporal_activities.policy import (
    EvaluatePromotionRequest,
    evaluate_promotion,
)

_ALLOWED_PROJECTS = {"AGENTDEV"}
_ALLOWED_REPOS = {"agentic-demo-service", "agentic-demo-greenfield"}
_BUNDLE_VERSION = "0.2.0"


def _policy_decision(inp: dict) -> dict:
    reasons: list[str] = []
    if inp["jira_project"] not in _ALLOWED_PROJECTS:
        reasons.append("jira_project_not_allowlisted")
    if inp["repository"] not in _ALLOWED_REPOS:
        reasons.append("repository_not_allowlisted")
    if inp["ci_status"] != "success":
        reasons.append("ci_not_successful")
    if not inp["scan_completed"]:
        reasons.append("scan_incomplete")
    if inp["blocking_findings"] > 0:
        reasons.append("blocking_security_findings")
    if inp["repair_attempts"] > inp["max_repair_attempts"]:
        reasons.append("repair_attempts_exceeded")
    if not inp["jira_snapshot_current"]:
        reasons.append("jira_snapshot_stale")
    if not inp["acceptance_verified"]:
        reasons.append("acceptance_not_verified")
    if not inp["pr_authorized"]:
        reasons.append("pr_not_authorized")
    if not inp["plan_approved"]:
        reasons.append("plan_not_approved")
    allow = len(reasons) == 0
    return {
        "allow": allow,
        "bundle_version": _BUNDLE_VERSION,
        "reasons": reasons,
        "next_action": "proceed" if allow else "block_and_report",
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
    blocking_findings=0,
    scan_completed=True,
    plan_approved=True,
    repair_attempts=0,
    max_repair_attempts=2,
    jira_snapshot_current=True,
    acceptance_verified=True,
    pr_authorized=True,
)


def _compliant(**overrides) -> PolicyInput:
    return replace(_COMPLIANT, **overrides)


async def test_compliant_change_is_allowed():
    decision = await _client().evaluate(_compliant())

    assert decision.allow is True
    assert decision.reasons == ()
    assert decision.next_action == "proceed"
    assert decision.bundle_version == _BUNDLE_VERSION


async def test_missing_ci_success_denies():
    decision = await _client().evaluate(_compliant(ci_status="failure"))

    assert decision.allow is False
    assert decision.next_action == "block_and_report"
    assert "ci_not_successful" in decision.reasons


async def test_blocking_finding_over_threshold_denies():
    decision = await _client().evaluate(_compliant(blocking_findings=1))

    assert decision.allow is False
    assert "blocking_security_findings" in decision.reasons


async def test_stale_jira_snapshot_denies():
    decision = await _client().evaluate(_compliant(jira_snapshot_current=False))

    assert decision.allow is False
    assert "jira_snapshot_stale" in decision.reasons


async def test_missing_approval_denies():
    decision = await _client().evaluate(_compliant(plan_approved=False))

    assert decision.allow is False
    assert "plan_not_approved" in decision.reasons


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
    assert document["blocking_findings"] == 0
    assert set(document) == {
        "jira_project",
        "repository",
        "ci_status",
        "blocking_findings",
        "scan_completed",
        "plan_approved",
        "repair_attempts",
        "max_repair_attempts",
        "jira_snapshot_current",
        "acceptance_verified",
        "pr_authorized",
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
            blocking_findings=2,
            scan_completed=True,
            plan_approved=True,
            repair_attempts=0,
            max_repair_attempts=2,
            jira_snapshot_current=True,
            acceptance_verified=True,
            pr_authorized=True,
        )
    )

    assert result.allow is False
    assert result.next_action == "block_and_report"
    assert result.reasons == ["blocking_security_findings"]
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
        allow=False, bundle_version="x", reasons=("plan_not_approved",), next_action="block"
    )
    engine = MockPolicyEngine(seeded)

    assert await engine.evaluate(_compliant()) is seeded
