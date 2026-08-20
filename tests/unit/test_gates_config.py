"""Unit tests for the gates config schema, bundles, and loader."""

from __future__ import annotations

import pytest
from services.integration.credentials import ConfigError
from services.orchestration.gates.config import (
    SHIPPED_BUNDLES,
    parse_gates_config,
)
from services.orchestration.gates.contract import PAUSE_FOR_HUMAN, REVIEW, gate_from_callable
from services.orchestration.gates.registry import GateRegistry, builtin_registry


def test_bare_profile_yields_the_bundle_default():
    config = parse_gates_config({"profile": "private"})

    assert config.profile == "private"
    assert {spec.name for spec in config.enabled_gates()} == {
        "code-review",
        "security-review",
        "acceptance",
        "semgrep",
        "trivy",
    }
    assert config.gate("semgrep").required is False


def test_the_two_bundles_differ():
    private = parse_gates_config({"profile": "private"})
    government = parse_gates_config({"profile": "government"})

    assert private.gate("semgrep").required is False
    assert government.gate("semgrep").required is True


def test_an_explicit_entry_overrides_one_gate():
    config = parse_gates_config(
        {
            "profile": "private",
            "gates": [{"name": "semgrep", "required": True, "params": {"threshold": "medium"}}],
        }
    )

    assert config.gate("semgrep").required is True
    assert config.gate("semgrep").params == {"threshold": "medium"}
    assert config.gate("trivy").required is True


def test_per_stage_fail_fast_overrides():
    config = parse_gates_config({"profile": "government", "fail_fast": {"scanner": True}})

    assert config.is_fail_fast("scanner") is True
    assert config.is_fail_fast("review") is False


def test_unknown_profile_fails_closed():
    with pytest.raises(ConfigError):
        parse_gates_config({"profile": "bespoke"})


def test_cyclic_after_fails_closed():
    with pytest.raises(ConfigError):
        parse_gates_config(
            {
                "profile": "private",
                "gates": [
                    {"name": "code-review", "after": ["acceptance"]},
                    {"name": "acceptance", "after": ["code-review"]},
                ],
            }
        )


def test_enabled_gate_absent_from_registry_fails_closed():
    with pytest.raises(ConfigError):
        parse_gates_config(
            {"profile": "private", "gates": [{"name": "snyk", "enabled": True}]}
        )


def test_a_plugin_gate_resolves_against_a_supplied_registry():
    registry = builtin_registry()

    async def evaluate(context):
        raise AssertionError("not run")

    registry.register("snyk", lambda: gate_from_callable("snyk", REVIEW, evaluate))
    config = parse_gates_config(
        {"profile": "government", "gates": [{"name": "snyk", "enabled": True, "required": True}]},
        registry=registry,
    )

    assert config.gate("snyk").required is True


def test_pause_for_human_on_block_parses():
    config = parse_gates_config(
        {"profile": "private", "gates": [{"name": "code-review", "on_block": PAUSE_FOR_HUMAN}]}
    )

    assert config.gate("code-review").on_block == PAUSE_FOR_HUMAN


def test_invalid_on_block_fails_closed():
    with pytest.raises(ConfigError):
        parse_gates_config(
            {"profile": "private", "gates": [{"name": "code-review", "on_block": "email"}]}
        )


def test_plan_human_gate_needs_no_registry_entry():
    config = parse_gates_config(
        {"profile": "private", "gates": [{"name": "plan", "enabled": True}]}
    )

    assert config.gate("plan").enabled is True


def test_plan_human_gate_is_off_in_the_default_bundles():
    for profile in ("private", "government"):
        config = parse_gates_config({"profile": profile})
        assert config.gate("plan") is None


def test_shipped_bundles_are_registered_gates():
    known = builtin_registry().names()
    for bundle in SHIPPED_BUNDLES.values():
        for spec in bundle.gates:
            assert spec.name in known


def test_empty_registry_reports_no_names():
    assert GateRegistry().names() == frozenset()
