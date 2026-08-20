"""Unit tests for gate plugin discovery and fail-closed loading."""

from __future__ import annotations

import pytest
from services.orchestration.gates.config import GateSpec, parse_gates_config
from services.orchestration.gates.contract import (
    BLOCK,
    PASS,
    SCANNER,
    GateContext,
    GateResult,
    gate_from_callable,
)
from services.orchestration.gates.plugins import (
    PluginError,
    load_plugin_gate,
    registry_for_config,
    scanner_backends_for_config,
)

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

    async def scan(self, target):
        from adapters.interfaces.security_scanner import ScanFinding

        return [
            ScanFinding(
                finding_id="s1",
                scanner="snyk",
                kind="dependency",
                severity="high",
                title="vulnerable dependency",
                rule_id="SNYK-1",
            )
        ]


def _discover_with(*entry_points):
    return lambda group: list(entry_points)


def _context() -> GateContext:
    return GateContext(
        task_id="T-1", execution_id="exec-1", repo="agentic-demo-service", ref="a" * 40
    )


async def test_scanner_plugin_loads_and_blocks():
    spec = GateSpec(name="snyk", distribution="snyk-gate", entry_point="snyk")
    discover = _discover_with(_FakeEntryPoint("snyk", "snyk-gate", _SnykScanner))

    gate = load_plugin_gate(spec, discover=discover)
    result = await gate.evaluate(_context())

    assert gate.kind == SCANNER
    assert result.verdict == BLOCK
    assert [finding.rule_id for finding in result.findings] == ["SNYK-1"]


async def test_gate_plugin_used_directly_when_it_exposes_evaluate():
    async def evaluate(context: GateContext) -> GateResult:
        return GateResult(name="compliance", kind="review", verdict=PASS)

    def factory():
        return gate_from_callable("compliance", "review", evaluate)

    spec = GateSpec(name="compliance", distribution="compliance-gate")
    discover = _discover_with(_FakeEntryPoint("compliance", "compliance-gate", factory))

    gate = load_plugin_gate(spec, discover=discover)
    result = await gate.evaluate(_context())

    assert gate.kind == "review"
    assert result.verdict == PASS


def test_absent_plugin_fails_closed():
    spec = GateSpec(name="snyk", distribution="snyk-gate")
    with pytest.raises(PluginError):
        load_plugin_gate(spec, discover=_discover_with())


def test_wrong_distribution_fails_closed():
    spec = GateSpec(name="snyk", distribution="snyk-gate")
    discover = _discover_with(_FakeEntryPoint("snyk", "other-dist", _SnykScanner))
    with pytest.raises(PluginError):
        load_plugin_gate(spec, discover=discover)


def test_registry_for_config_registers_a_required_plugin():
    config = parse_gates_config(
        {
            "profile": "government",
            "gates": [
                {
                    "name": "snyk",
                    "enabled": True,
                    "required": True,
                    "plugin": {"distribution": "snyk-gate", "entry_point": "snyk"},
                }
            ],
        }
    )
    discover = _discover_with(_FakeEntryPoint("snyk", "snyk-gate", _SnykScanner))

    registry = registry_for_config(config, discover=discover)

    assert registry.has("snyk")
    assert registry.resolve("snyk").kind == SCANNER


def test_registry_for_config_fails_closed_on_required_absent_plugin():
    config = parse_gates_config(
        {
            "profile": "government",
            "gates": [
                {
                    "name": "snyk",
                    "enabled": True,
                    "required": True,
                    "plugin": {"distribution": "snyk-gate"},
                }
            ],
        }
    )
    with pytest.raises(PluginError):
        registry_for_config(config, discover=_discover_with())


def test_registry_for_config_skips_an_absent_advisory_plugin():
    config = parse_gates_config(
        {
            "profile": "government",
            "gates": [
                {
                    "name": "snyk",
                    "enabled": True,
                    "required": False,
                    "plugin": {"distribution": "snyk-gate"},
                }
            ],
        }
    )

    registry = registry_for_config(config, discover=_discover_with())

    assert not registry.has("snyk")


def _backend_names(backends) -> list[str]:
    return [backend.name for backend in backends]


def test_scanner_backends_follow_the_enabled_builtins():
    config = parse_gates_config(
        {"profile": "government", "gates": [{"name": "semgrep", "enabled": False}]}
    )

    backends = scanner_backends_for_config(config, discover=_discover_with())

    assert _backend_names(backends) == ["trivy"]


def test_scanner_backends_include_an_enabled_plugin_scanner():
    config = parse_gates_config(
        {
            "profile": "government",
            "gates": [
                {"name": "semgrep", "enabled": False},
                {"name": "trivy", "enabled": False},
                {
                    "name": "snyk",
                    "enabled": True,
                    "required": True,
                    "plugin": {"distribution": "snyk-gate", "entry_point": "snyk"},
                },
            ],
        }
    )
    discover = _discover_with(_FakeEntryPoint("snyk", "snyk-gate", _SnykScanner))

    backends = scanner_backends_for_config(config, discover=discover)

    assert _backend_names(backends) == ["snyk"]


def test_scanner_backends_exclude_review_plugins():
    def factory():
        async def evaluate(context: GateContext) -> GateResult:
            return GateResult(name="compliance", kind="review", verdict=PASS)

        return gate_from_callable("compliance", "review", evaluate)

    config = parse_gates_config(
        {
            "profile": "government",
            "gates": [
                {"name": "semgrep", "enabled": False},
                {"name": "trivy", "enabled": False},
                {
                    "name": "compliance",
                    "enabled": True,
                    "required": True,
                    "plugin": {"distribution": "compliance-gate"},
                },
            ],
        }
    )
    discover = _discover_with(_FakeEntryPoint("compliance", "compliance-gate", factory))

    backends = scanner_backends_for_config(config, discover=discover)

    assert backends == ()


def _engine(name: str = "ai-security-review", **kw):
    from services.orchestration.gates.engine_source import GateEngineConfig

    return GateEngineConfig(
        name=name, enabled=True, mode="local", local_path="/tmp/engine", **kw
    )


def _registry_with_engine(*engines):
    from services.orchestration.gates.builtin import engine_gate
    from services.orchestration.gates.registry import builtin_registry

    registry = builtin_registry()
    for engine in engines:
        registry.register(engine.name, lambda engine=engine: engine_gate(engine))
    return registry


def test_engine_gate_builds_a_backend_when_enabled():
    engine = _engine()
    config = parse_gates_config(
        {
            "profile": "government",
            "gates": [
                {"name": "semgrep", "enabled": False},
                {"name": "trivy", "enabled": False},
                {
                    "name": "ai-security-review",
                    "enabled": True,
                    "required": True,
                    "params": {"profile": "cms-ars"},
                },
            ],
        },
        registry=_registry_with_engine(engine),
    )

    backends = scanner_backends_for_config(config, discover=_discover_with(), engines=(engine,))

    assert _backend_names(backends) == ["ai-security-review"]


def test_engine_gate_enabled_without_a_configured_engine_fails_closed():
    from services.integration.credentials import ConfigError

    with pytest.raises(ConfigError):
        parse_gates_config(
            {
                "profile": "government",
                "gates": [
                    {"name": "semgrep", "enabled": False},
                    {"name": "trivy", "enabled": False},
                    {"name": "ai-security-review", "enabled": True, "required": True},
                ],
            }
        )


def test_a_second_engine_registers_its_own_gate():
    reviewer = _engine("ai-security-review")
    sonar = _engine("sonarqube-review")
    config = parse_gates_config(
        {
            "profile": "government",
            "gates": [
                {"name": "semgrep", "enabled": False},
                {"name": "trivy", "enabled": False},
                {"name": "ai-security-review", "enabled": True, "required": True},
                {"name": "sonarqube-review", "enabled": True, "required": True},
            ],
        },
        registry=_registry_with_engine(reviewer, sonar),
    )

    backends = scanner_backends_for_config(
        config, discover=_discover_with(), engines=(reviewer, sonar)
    )

    assert _backend_names(backends) == ["ai-security-review", "sonarqube-review"]

