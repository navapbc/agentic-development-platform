"""A registry that resolves gates by name."""

from __future__ import annotations

from typing import Any

from services.orchestration.gates.contract import Gate, GateFactory


class GateNotFoundError(KeyError):
    """A gate name is not registered."""


class DuplicateGateError(ValueError):
    """A gate name is registered more than once."""


class GateRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, GateFactory] = {}

    def register(self, name: str, factory: GateFactory) -> None:
        if name in self._factories:
            raise DuplicateGateError(f"gate {name!r} is already registered")
        self._factories[name] = factory

    def has(self, name: str) -> bool:
        return name in self._factories

    def resolve(self, name: str) -> Gate:
        try:
            factory = self._factories[name]
        except KeyError as exc:
            raise GateNotFoundError(f"gate {name!r} is not registered") from exc
        return factory()

    def names(self) -> frozenset[str]:
        return frozenset(self._factories)


def builtin_registry() -> GateRegistry:
    from services.orchestration.gates.builtin import builtin_gates

    registry = GateRegistry()
    for name, factory in builtin_gates().items():
        registry.register(name, factory)
    return registry


def _engine_map(config: Any, settings: Any, engines: Any) -> dict[str, Any]:
    if engines is None:
        from services.orchestration.gates.engine_source import (
            enabled_engines,
            load_gate_engine_configs,
        )

        engines = enabled_engines(load_gate_engine_configs(settings))
    named = {spec.name for spec in config.enabled_gates()}
    return {engine.name: engine for engine in engines if engine.name in named}


def registry_for_config(
    config: Any,
    *,
    settings: Any = None,
    engines: Any = None,
) -> GateRegistry:
    """Build the gate registry from config alone: built-ins plus config-sourced engines."""
    from services.orchestration.gates.builtin import engine_gate

    registry = builtin_registry()
    for engine in _engine_map(config, settings, engines).values():
        registry.register(engine.name, lambda engine=engine: engine_gate(engine))
    return registry


def scanner_backends_for_config(
    config: Any,
    *,
    settings: Any = None,
    engines: Any = None,
) -> tuple[Any, ...]:
    """Resolve the scanner backends for the enabled gates from config alone."""
    from services.orchestration.gates.builtin import (
        builtin_scanner_backends,
        engine_review_backend,
    )

    builtins = builtin_scanner_backends()
    engine_map = _engine_map(config, settings, engines)
    backends: list[Any] = []
    for spec in config.enabled_gates():
        if spec.name in engine_map:
            backends.append(
                engine_review_backend(engine_map[spec.name], str(spec.params.get("profile", "")))
            )
        elif spec.name in builtins:
            backends.append(builtins[spec.name]())
    return tuple(backends)
