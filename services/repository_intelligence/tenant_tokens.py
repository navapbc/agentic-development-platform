"""The bearer tokens that bind a repository intelligence request to one tenant.

A tenant's activity worker sends its token as REPO_INTELLIGENCE_TOKEN. Repository intelligence
holds every tenant's token, each in REPO_INTELLIGENCE_TOKEN_<TENANT> in AWS, or derives them from
REPO_INTELLIGENCE_TOKEN_KEY on a workstation, and answers a request only for the repositories of
the tenant whose token it carries.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from collections.abc import Iterable, Mapping

ENV_PREFIX = "REPO_INTELLIGENCE_TOKEN_"
_DERIVATION_LABEL = "repo-intelligence:"


def token_env_name(tenant: str) -> str:
    return f"{ENV_PREFIX}{tenant.upper()}"


def derive_token(key: str, tenant: str) -> str:
    return hmac.new(
        key.encode(), f"{_DERIVATION_LABEL}{tenant}".encode(), hashlib.sha256
    ).hexdigest()


def tenant_tokens(
    tenants: Iterable[str], key: str = "", environ: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Return each tenant's token, from its own variable or else derived from the key."""
    values = os.environ if environ is None else environ
    tokens: dict[str, str] = {}
    for tenant in tenants:
        token = values.get(token_env_name(tenant), "").strip()
        if not token and key:
            token = derive_token(key, tenant)
        if token:
            tokens[tenant] = token
    return tokens


def tenant_for_token(tokens: Mapping[str, str], presented: str) -> str | None:
    """Return the tenant whose token was presented, comparing every token in constant time."""
    match = None
    for tenant, token in tokens.items():
        if hmac.compare_digest(token.encode(), presented.encode()):
            match = tenant
    return match
