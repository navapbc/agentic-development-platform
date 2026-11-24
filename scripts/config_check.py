"""Report each config file version against the version this release expects.

The platform files are read from config/local/, and each tenant-only file from the folder of the
tenant this process serves, or of every local tenant when it serves none.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence

from services.integration.credentials import (
    ConfigError,
    EnvSecretResolver,
    SecretNotFoundError,
    SecretResolver,
    config_status,
    config_template,
    env_config_dir,
)
from services.integration.tenant_config import (
    TENANT_ONLY_FILES,
    pinned,
    refuse_platform_tenant_files,
)
from services.orchestration.escalation import load_escalation_config

from scripts.config import Settings, get_settings
from scripts.env_file import tenant_env_path
from scripts.local_tenants import TenantLayoutError, local_tenant_names

_CONFIGS = (
    "github",
    "jira",
    "integrations",
    "gates",
    "gate_engine",
    "tickets",
    "tickets_isolation",
    "repositories",
    "registries",
    "profiles",
    "image_policy",
    "image_exceptions",
    "escalation",
)
_CONTENT_CHECKS: dict[str, Callable[[Settings], object]] = {
    "escalation": load_escalation_config,
}
_PLATFORM_SECRETS = ("COORDINATION_SHARED_SECRET", "REPO_INTELLIGENCE_TOKEN_KEY")
_TENANT_SECRETS = ("MCP_GATEWAY_TOKEN",)
_REQUIRED_SECRETS = (*_PLATFORM_SECRETS, *_TENANT_SECRETS)


def report_required_secrets(
    resolver: SecretResolver, refs: Sequence[str] = _REQUIRED_SECRETS, label: str = ""
) -> int:
    """Print the state of each secret a service cannot serve without, and count the absent."""
    missing = 0
    for ref in refs:
        try:
            resolver.resolve(ref)
        except SecretNotFoundError as exc:
            print(f"[config-check] {label}{ref}: MISSING, {exc}")
            missing += 1
        else:
            print(f"[config-check] {label}{ref}: set")
    return missing


def _tenants(settings: Settings) -> list[str]:
    """Return the tenant this process serves, or every local tenant when it serves none."""
    if settings.tenant:
        return [settings.tenant]
    return local_tenant_names(env_config_dir())


def _report(name: str, label: str, settings: Settings) -> int:
    """Print the state of one config file, and return 1 when it needs the operator's attention."""
    try:
        status = config_status(name)
    except ConfigError as exc:
        print(f"[config-check] {label}: {exc}")
        return 1
    failed = 0
    template = config_template(name).relative_to(env_config_dir())
    if not status.example_matches_current:
        print(
            f"[config-check] {label}: TEMPLATE DRIFT, {template} is version "
            f"{status.example_version} but the code constant is {status.current_version}. "
            "Reconcile the template and _CURRENT_CONFIG_VERSIONS."
        )
        failed = 1
    if not status.present:
        print(f"[config-check] {label}: not created (copy config/local/{template})")
        return failed
    if not status.up_to_date:
        keys = ", ".join(status.new_keys) or "(no new keys)"
        print(
            f"[config-check] {label}: OUTDATED, version {status.operator_version} < "
            f"{status.current_version}. Add: {keys}"
        )
        return 1
    check = _CONTENT_CHECKS.get(name)
    try:
        if check is not None:
            check(settings)
    except ConfigError as exc:
        print(f"[config-check] {label}: INVALID, {exc}")
        return 1
    print(f"[config-check] {label}: up to date (version {status.operator_version})")
    return failed


def main() -> int:
    settings = get_settings()
    outdated = 0
    platform_dir = env_config_dir()
    try:
        refuse_platform_tenant_files(platform_dir)
    except ConfigError as exc:
        print(f"[config-check] {exc}")
        outdated += 1
    for name in _CONFIGS:
        if name not in TENANT_ONLY_FILES:
            outdated += _report(name, name, settings)
    try:
        tenants = _tenants(settings)
    except TenantLayoutError as exc:
        print(f"[config-check] {exc}")
        tenants = []
        outdated += 1
    for tenant in tenants:
        with pinned(platform_dir / "tenants" / tenant):
            for name in _CONFIGS:
                if name in TENANT_ONLY_FILES:
                    outdated += _report(name, f"tenants/{tenant}/{name}", settings)
        outdated += report_required_secrets(
            EnvSecretResolver(env_file=tenant_env_path(tenant)),
            _TENANT_SECRETS,
            f"tenants/{tenant}/",
        )
    outdated += report_required_secrets(EnvSecretResolver(), _PLATFORM_SECRETS)
    return 1 if outdated else 0


if __name__ == "__main__":
    sys.exit(main())
