"""Activate the tenant's published config bundle in an operator command."""

from __future__ import annotations

from typing import Any


def activate_tenant_bundle(settings: Any = None) -> None:
    """Make this process read tenant-only files from the tenant's latest published bundle.

    A deployed worker reads its tenant's bundle from the config bucket, and a process resolves
    tenant-only files from that bucket only after it configures the tenant config. An operator
    command run in the worker container is such a process. A command without a bucket already
    reads the tenant's directory, so it configures nothing. A bundle that fails validation raises
    here without the worker's Slack rejection alert.
    """
    from services.integration import tenant_config

    from scripts.config import get_settings

    resolved = settings or get_settings()
    if tenant_config.active_tenant_config() is not None or not resolved.tenant_config_bucket:
        return
    tenant_config.configure(resolved)
