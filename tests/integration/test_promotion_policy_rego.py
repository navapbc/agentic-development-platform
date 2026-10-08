"""Container-gated evaluation of the promotion policy against the pinned OPA image.

Runs the authored `config/policies/promotion.rego` through the same
`openpolicyagent/opa:1.20.1` image the compose stack serves, so the data-driven
gate rules are exercised rather than reimplemented. Skipped when no container
runtime (docker, podman, or nerdctl) is available.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_OPA_IMAGE = "openpolicyagent/opa:1.20.1"
_POLICIES = Path(__file__).resolve().parents[2] / "config" / "policies"
_RUNTIMES = ("docker", "podman", "nerdctl")


def _container_runtime() -> str | None:
    for runtime in _RUNTIMES:
        if shutil.which(runtime) is not None:
            return runtime
    return None


_RUNTIME = _container_runtime()

pytestmark = pytest.mark.skipif(
    _RUNTIME is None,
    reason="a container runtime (docker, podman, or nerdctl) is required to evaluate the rego",
)


def _decision(document: dict) -> dict:
    payload = json.dumps(document)
    result = subprocess.run(
        [
            _RUNTIME,
            "run",
            "--rm",
            "--platform",
            "linux/amd64",
            "-i",
            "-v",
            f"{_POLICIES}:/policies:ro",
            _OPA_IMAGE,
            "eval",
            "-d",
            "/policies",
            "-I",
            "data.agentic.promotion.decision",
        ],
        input=payload,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    return parsed["result"][0]["expressions"][0]["value"]


def _compliant(**overrides) -> dict:
    document = {
        "jira_project": "AGENTDEV",
        "repository": "agentic-demo-service",
        "ci_status": "success",
        "repair_attempts": 0,
        "max_repair_attempts": 2,
        "jira_snapshot_current": True,
        "pr_authorized": True,
        "gates": [
            {"name": "code-review", "required": True, "verdict": "pass"},
            {"name": "semgrep", "required": True, "verdict": "pass"},
        ],
        "required_gates": ["code-review", "semgrep"],
    }
    document.update(overrides)
    return document


def test_compliant_change_is_allowed():
    decision = _decision(_compliant())
    assert decision["allow"] is True
    assert decision["reasons"] == []
    assert decision["human_review"] == []
    assert decision["bundle_version"] == "0.4.0"
    assert decision["next_action"] == "proceed"


def test_a_required_blocking_gate_denies():
    document = _compliant(
        gates=[{"name": "code-review", "required": True, "verdict": "block"}],
        required_gates=["code-review"],
    )
    decision = _decision(document)
    assert decision["allow"] is False
    assert "gate_failed:code-review" in decision["reasons"]


def test_an_advisory_blocking_gate_does_not_deny():
    document = _compliant(
        gates=[
            {"name": "code-review", "required": True, "verdict": "pass"},
            {"name": "semgrep", "required": False, "verdict": "block"},
        ],
        required_gates=["code-review"],
    )
    decision = _decision(document)
    assert decision["allow"] is True
    assert decision["reasons"] == []


def test_a_required_gate_that_did_not_run_fails_closed():
    document = _compliant(
        gates=[{"name": "code-review", "required": True, "verdict": "pass"}],
        required_gates=["code-review", "trivy"],
    )
    decision = _decision(document)
    assert decision["allow"] is False
    assert "required_gate_missing:trivy" in decision["reasons"]


def test_a_plugin_gate_needs_no_rego_edit():
    document = _compliant(
        gates=[{"name": "snyk", "required": True, "verdict": "block"}],
        required_gates=["snyk"],
    )
    decision = _decision(document)
    assert decision["allow"] is False
    assert "gate_failed:snyk" in decision["reasons"]


def test_a_deployment_invariant_still_denies():
    decision = _decision(_compliant(ci_status="failed"))
    assert decision["allow"] is False
    assert "ci_not_successful" in decision["reasons"]


def test_input_allowlists_admit_a_backend_the_defaults_omit():
    document = _compliant(
        jira_project="PFMLPB",
        repository="archive-massgov-pfml",
        allowed_projects=["PFMLPB"],
        allowed_repos=["archive-massgov-pfml"],
        ci_status="skipped",
        ci_required=False,
    )
    decision = _decision(document)
    assert decision["allow"] is True
    assert decision["reasons"] == []


def test_ci_not_required_skips_the_ci_gate():
    decision = _decision(_compliant(ci_status="skipped", ci_required=False))
    assert decision["allow"] is True
    assert "ci_not_successful" not in decision["reasons"]


def test_a_disabled_gate_drops_from_the_decision():
    document = _compliant(
        gates=[{"name": "code-review", "required": True, "verdict": "pass"}],
        required_gates=["code-review"],
    )
    decision = _decision(document)
    assert decision["allow"] is True
    assert decision["reasons"] == []


def test_a_pause_for_human_gate_routes_to_human_review():
    document = _compliant(
        gates=[
            {
                "name": "security-review",
                "required": True,
                "verdict": "block",
                "on_block": "pause_for_human",
            }
        ],
        required_gates=["security-review"],
    )
    decision = _decision(document)
    assert decision["allow"] is False
    assert decision["reasons"] == []
    assert "gate_failed:security-review" in decision["human_review"]
    assert decision["next_action"] == "await_human"


def test_a_fail_closed_gate_denies_even_beside_a_pause_gate():
    document = _compliant(
        gates=[
            {"name": "code-review", "required": True, "verdict": "block"},
            {
                "name": "security-review",
                "required": True,
                "verdict": "block",
                "on_block": "pause_for_human",
            },
        ],
        required_gates=["code-review", "security-review"],
    )
    decision = _decision(document)
    assert decision["allow"] is False
    assert "gate_failed:code-review" in decision["reasons"]
    assert "gate_failed:security-review" in decision["human_review"]
    assert decision["next_action"] == "block_and_report"


def test_skipped_ci_reports_ci_skipped():
    decision = _decision(_compliant(ci_status="skipped"))
    assert decision["allow"] is False
    assert "ci_skipped" in decision["reasons"]
    assert "ci_not_successful" not in decision["reasons"]
