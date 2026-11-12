"""Report each per-environment config file version against the version this release expects."""

from __future__ import annotations

import sys

from services.integration.credentials import (
    ConfigError,
    EnvSecretResolver,
    SecretNotFoundError,
    SecretResolver,
    config_status,
)

from scripts.config import get_settings

_CONFIGS = ("github", "jira", "integrations", "gates", "gate_engine", "tickets")
_REQUIRED_SECRETS = ("COORDINATION_SHARED_SECRET",)


def report_required_secrets(resolver: SecretResolver) -> int:
    """Print the state of each secret a service refuses to start without, and count the absent."""
    missing = 0
    for ref in _REQUIRED_SECRETS:
        try:
            resolver.resolve(ref)
        except SecretNotFoundError as exc:
            print(f"[config-check] {ref}: MISSING, {exc}")
            missing += 1
        else:
            print(f"[config-check] {ref}: set")
    return missing


def main() -> int:
    env = get_settings().environment
    outdated = 0
    for name in _CONFIGS:
        try:
            status = config_status(name, environment=env)
        except ConfigError as exc:
            print(f"[config-check] {name}: {exc}")
            outdated += 1
            continue
        if not status.example_matches_current:
            print(
                f"[config-check] {name}: TEMPLATE DRIFT, {name}.yaml.example is version "
                f"{status.example_version} but the code constant is {status.current_version}. "
                "Reconcile the template and _CURRENT_CONFIG_VERSIONS."
            )
            outdated += 1
        if not status.present:
            print(f"[config-check] {name}: not created (copy config/{env}/{name}.yaml.example)")
            continue
        if status.up_to_date:
            print(f"[config-check] {name}: up to date (version {status.operator_version})")
            continue
        keys = ", ".join(status.new_keys) or "(no new keys)"
        print(
            f"[config-check] {name}: OUTDATED, version {status.operator_version} < "
            f"{status.current_version}. Add: {keys}"
        )
        outdated += 1
    outdated += report_required_secrets(EnvSecretResolver())
    return 1 if outdated else 0


if __name__ == "__main__":
    sys.exit(main())
