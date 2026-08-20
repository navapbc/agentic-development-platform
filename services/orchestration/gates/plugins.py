"""Entry-point discovery of out-of-repo gates, bound only to config-named distributions."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from services.integration.credentials import ConfigError
from services.orchestration.gates.builtin import scanner_gate
from services.orchestration.gates.config import GatesConfig, GateSpec
from services.orchestration.gates.contract import Gate
from services.orchestration.gates.registry import GateRegistry, builtin_registry

GATE_ENTRYPOINT_GROUP = "agentic_dev_platform.gates"

DiscoverFn = Callable[[str], Iterable[Any]]


class PluginError(ConfigError):
    """A configured gate plugin is absent, ambiguous, or fails to load."""


def _discover(group: str) -> Iterable[Any]:
    from importlib.metadata import entry_points

    return entry_points(group=group)


def _distribution_name(entry_point: Any) -> str:
    dist = getattr(entry_point, "dist", None)
    return getattr(dist, "name", "") if dist is not None else ""


def _adapt(spec: GateSpec, instance: Any) -> Gate:
    if hasattr(instance, "evaluate"):
        return instance
    if hasattr(instance, "scan"):
        return scanner_gate(spec.name, lambda: instance)
    raise PluginError(
        f"gate {spec.name!r} plugin must expose evaluate or scan, got {type(instance).__name__}"
    )


def _load_plugin_instance(spec: GateSpec, *, discover: DiscoverFn = _discover) -> Any:
    if not spec.is_plugin:
        raise PluginError(f"gate {spec.name!r} is not a plugin")
    wanted = spec.entry_point or spec.name
    matches = [
        entry_point
        for entry_point in discover(GATE_ENTRYPOINT_GROUP)
        if entry_point.name == wanted and _distribution_name(entry_point) == spec.distribution
    ]
    if not matches:
        raise PluginError(
            f"gate {spec.name!r} plugin {wanted!r} not found in distribution "
            f"{spec.distribution!r}"
        )
    if len(matches) > 1:
        raise PluginError(
            f"gate {spec.name!r} plugin {wanted!r} is ambiguous in distribution "
            f"{spec.distribution!r}"
        )
    try:
        loaded = matches[0].load()
        return loaded()
    except Exception as exc:  # noqa: BLE001
        raise PluginError(f"gate {spec.name!r} plugin failed to load: {exc}") from exc


def load_plugin_gate(spec: GateSpec, *, discover: DiscoverFn = _discover) -> Gate:
    return _adapt(spec, _load_plugin_instance(spec, discover=discover))


def _engine_map(
    config: GatesConfig, settings: Any, engines: Any
) -> dict[str, Any]:
    if engines is None:
        from services.orchestration.gates.engine_source import (
            enabled_engines,
            load_gate_engine_configs,
        )

        engines = enabled_engines(load_gate_engine_configs(settings))
    named = {c.name for c in config.enabled_gates()}
    return {c.name: c for c in engines if c.name in named}


def scanner_backends_for_config(
    config: GatesConfig,
    *,
    discover: DiscoverFn = _discover,
    settings: Any = None,
    engines: Any = None,
) -> tuple[Any, ...]:
    from services.orchestration.gates.builtin import (
        builtin_scanner_backends,
        engine_review_backend,
    )

    builtins = builtin_scanner_backends()
    engine_map = _engine_map(config, settings, engines)
    backends: list[Any] = []
    for spec in config.enabled_gates():
        if spec.is_plugin:
            try:
                instance = _load_plugin_instance(spec, discover=discover)
            except PluginError:
                if spec.required:
                    raise
                continue
            if hasattr(instance, "scan"):
                backends.append(instance)
        elif spec.name in engine_map:
            backends.append(
                engine_review_backend(engine_map[spec.name], str(spec.params.get("profile", "")))
            )
        elif spec.name in builtins:
            backends.append(builtins[spec.name]())
    return tuple(backends)


def registry_for_config(
    config: GatesConfig,
    *,
    discover: DiscoverFn = _discover,
    settings: Any = None,
    engines: Any = None,
) -> GateRegistry:
    from services.orchestration.gates.builtin import engine_gate

    registry = builtin_registry()
    engine_map = _engine_map(config, settings, engines)
    for engine in engine_map.values():
        registry.register(engine.name, lambda engine=engine: engine_gate(engine))
    for spec in config.enabled_gates():
        if not spec.is_plugin:
            continue
        try:
            gate = load_plugin_gate(spec, discover=discover)
        except PluginError:
            if spec.required:
                raise
            continue
        registry.register(spec.name, lambda gate=gate: gate)
    return registry
