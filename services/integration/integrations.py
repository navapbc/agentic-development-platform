"""Non-secret integration delivery settings from config/<env>/integrations.yaml."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from scripts.config import Settings, get_settings

from services.integration.credentials import (
    ConfigError,
    load_env_config,
    require_current_config,
)

POLLING = "polling"
WEBHOOK = "webhook"
HYBRID = "hybrid"
_VALID_MODES = frozenset({POLLING, WEBHOOK, HYBRID})
_DEFAULT_MODE = POLLING


@dataclass(frozen=True)
class IntegrationsConfig:
    modes: dict[str, str]

    def mode(self, name: str) -> str:
        return self.modes.get(name, _DEFAULT_MODE)

    def webhook_enabled(self, name: str) -> bool:
        return self.mode(name) in (WEBHOOK, HYBRID)


def parse_integrations_config(raw: Mapping[str, Any]) -> IntegrationsConfig:
    modes_raw = raw.get("modes") or {}
    if not isinstance(modes_raw, Mapping):
        raise ConfigError("integrations.modes must be a mapping")
    modes: dict[str, str] = {}
    for name, value in modes_raw.items():
        if value not in _VALID_MODES:
            raise ConfigError(
                f"integrations.modes.{name} must be one of {sorted(_VALID_MODES)}, got {value!r}"
            )
        modes[str(name)] = str(value)
    return IntegrationsConfig(modes=modes)


def load_integrations_config(
    settings: Settings | None = None, *, environment: str | None = None
) -> IntegrationsConfig:
    resolved = settings or get_settings()
    env = environment or resolved.environment
    require_current_config("integrations", environment=env)
    raw = load_env_config("integrations", environment=env)
    return parse_integrations_config(raw)
