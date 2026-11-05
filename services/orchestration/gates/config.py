"""Gate selection from config/<env>/gates.yaml, resolved over shipped bundles."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from scripts.config import Settings, get_settings

from services.integration.credentials import (
    ConfigError,
    load_env_config,
    require_current_config,
)
from services.orchestration.gates.contract import FAIL_CLOSED, PAUSE_FOR_HUMAN
from services.orchestration.gates.registry import GateRegistry, builtin_registry

_ON_BLOCK = frozenset({FAIL_CLOSED, PAUSE_FOR_HUMAN})

PLAN_GATE = "plan"
HUMAN_GATES = frozenset({PLAN_GATE})


@dataclass(frozen=True)
class GateSpec:
    name: str
    enabled: bool = True
    required: bool = True
    after: tuple[str, ...] = ()
    on_block: str = FAIL_CLOSED
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GatesConfig:
    profile: str
    gates: tuple[GateSpec, ...]
    fail_fast: dict[str, bool] = field(default_factory=dict)

    def gate(self, name: str) -> GateSpec | None:
        for spec in self.gates:
            if spec.name == name:
                return spec
        return None

    def enabled_gates(self) -> tuple[GateSpec, ...]:
        return tuple(spec for spec in self.gates if spec.enabled)

    def is_fail_fast(self, stage: str) -> bool:
        return bool(self.fail_fast.get(stage, False))


def _spec(name: str, *, required: bool = True) -> GateSpec:
    return GateSpec(name=name, enabled=True, required=required)


SHIPPED_BUNDLES: dict[str, GatesConfig] = {
    "private": GatesConfig(
        profile="private",
        gates=(
            _spec("code-review"),
            _spec("security-review"),
            _spec("acceptance"),
            _spec("semgrep", required=False),
            _spec("trivy"),
        ),
        fail_fast={"review": False, "scanner": False},
    ),
    "government": GatesConfig(
        profile="government",
        gates=(
            _spec("code-review"),
            _spec("security-review"),
            _spec("acceptance"),
            _spec("semgrep"),
            _spec("trivy"),
        ),
        fail_fast={"review": False, "scanner": False},
    ),
}


def _parse_override(raw: Mapping[str, Any], base: GateSpec | None) -> GateSpec:
    name = str(raw["name"])
    start = base or GateSpec(name=name)
    after_raw = raw.get("after", start.after)
    if not isinstance(after_raw, list | tuple):
        raise ConfigError(f"gates.{name}.after must be a list")
    on_block = str(raw.get("on_block", start.on_block))
    if on_block not in _ON_BLOCK:
        raise ConfigError(
            f"gates.{name}.on_block must be one of {sorted(_ON_BLOCK)}, got {on_block!r}"
        )
    params = raw.get("params", start.params)
    if not isinstance(params, Mapping):
        raise ConfigError(f"gates.{name}.params must be a mapping")
    return replace(
        start,
        name=name,
        enabled=bool(raw.get("enabled", start.enabled)),
        required=bool(raw.get("required", start.required)),
        after=tuple(str(item) for item in after_raw),
        on_block=on_block,
        params=dict(params),
    )


def _assert_acyclic(gates: tuple[GateSpec, ...]) -> None:
    edges = {spec.name: set(spec.after) for spec in gates}
    visiting: set[str] = set()
    done: set[str] = set()

    def visit(node: str) -> None:
        if node in done:
            return
        if node in visiting:
            raise ConfigError(f"gates config has a cyclic after edge at {node!r}")
        visiting.add(node)
        for dep in edges.get(node, set()):
            visit(dep)
        visiting.discard(node)
        done.add(node)

    for spec in gates:
        visit(spec.name)


def parse_gates_config(
    raw: Mapping[str, Any], *, registry: GateRegistry | None = None
) -> GatesConfig:
    profile = raw.get("profile")
    if profile not in SHIPPED_BUNDLES:
        raise ConfigError(
            f"gates.profile must be one of {sorted(SHIPPED_BUNDLES)}, got {profile!r}"
        )
    base = SHIPPED_BUNDLES[str(profile)]

    overrides_raw = raw.get("gates", [])
    if not isinstance(overrides_raw, list | tuple):
        raise ConfigError("gates must be a list")

    merged: dict[str, GateSpec] = {spec.name: spec for spec in base.gates}
    order = [spec.name for spec in base.gates]
    for entry in overrides_raw:
        if not isinstance(entry, Mapping) or "name" not in entry:
            raise ConfigError("each gate entry must be a mapping with a name")
        name = str(entry["name"])
        merged[name] = _parse_override(entry, merged.get(name))
        if name not in order:
            order.append(name)

    gates = tuple(merged[name] for name in order)
    _assert_acyclic(gates)

    resolver = registry or builtin_registry()
    known = resolver.names()
    for spec in gates:
        if spec.name in HUMAN_GATES:
            continue
        if spec.enabled and spec.name not in known:
            raise ConfigError(f"gate {spec.name!r} is enabled but not registered")

    fail_fast = dict(base.fail_fast)
    fail_fast_raw = raw.get("fail_fast", {})
    if not isinstance(fail_fast_raw, Mapping):
        raise ConfigError("gates.fail_fast must be a mapping")
    for stage, value in fail_fast_raw.items():
        fail_fast[str(stage)] = bool(value)

    return GatesConfig(
        profile=str(profile),
        gates=gates,
        fail_fast=fail_fast,
    )


def load_gates_config(
    settings: Settings | None = None,
    *,
    environment: str | None = None,
    registry: GateRegistry | None = None,
) -> GatesConfig:
    resolved = settings or get_settings()
    env = environment or resolved.environment
    require_current_config("gates", environment=env)
    raw = load_env_config("gates", environment=env)
    resolved_registry = registry or _registry_with_engines(resolved, env)
    return parse_gates_config(raw, registry=resolved_registry)


def _registry_with_engines(settings: Settings, environment: str) -> GateRegistry:
    from services.orchestration.gates.builtin import engine_gate
    from services.orchestration.gates.engine_source import (
        enabled_engines,
        load_gate_engine_configs,
    )

    registry = builtin_registry()
    for engine in enabled_engines(load_gate_engine_configs(settings, environment=environment)):
        registry.register(engine.name, lambda engine=engine: engine_gate(engine))
    return registry
