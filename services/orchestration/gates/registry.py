"""A registry that resolves gates by name."""

from __future__ import annotations

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
