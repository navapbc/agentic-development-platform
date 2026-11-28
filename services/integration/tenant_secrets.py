"""Each tenant's own secret, named <PREFIX><TENANT> in the environment of a multi-tenant process.

A tenant without its own variable has no secret, even when it is the one tenant served, because
no secret belongs to the platform on a tenant's behalf.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping, Sequence

SLACK_SIGNING_ENV_PREFIX = "SLACK_SIGNING_SECRET_"
ESCALATION_ACTION_KEY_ENV_PREFIX = "ESCALATION_ACTION_KEY_"
ESCALATION_ACTION_KEY_SHA256_ENV_PREFIX = "ESCALATION_ACTION_KEY_SHA256_"


def secret_env_name(prefix: str, tenant: str) -> str:
    return f"{prefix}{tenant.upper()}"


def tenant_secrets(
    tenants: Sequence[str], prefix: str, environ: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Return the secret under the prefix of each tenant that has one."""
    values = os.environ if environ is None else environ
    secrets: dict[str, str] = {}
    for tenant in tenants:
        own = values.get(secret_env_name(prefix, tenant), "").strip() if tenant else ""
        if own:
            secrets[tenant] = own
    return secrets


def action_key_fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def action_key_fingerprints(
    tenants: Sequence[str], environ: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Return the SHA-256 fingerprint of each tenant's escalation action key.

    A process that holds a tenant's key fingerprints it. The Slack posting service holds no key,
    so it reads each tenant's fingerprint from ESCALATION_ACTION_KEY_SHA256_<TENANT>.
    """
    fingerprints = {
        tenant: value.lower()
        for tenant, value in tenant_secrets(
            tenants, ESCALATION_ACTION_KEY_SHA256_ENV_PREFIX, environ
        ).items()
    }
    keys = tenant_secrets(tenants, ESCALATION_ACTION_KEY_ENV_PREFIX, environ)
    fingerprints.update({tenant: action_key_fingerprint(key) for tenant, key in keys.items()})
    return fingerprints
