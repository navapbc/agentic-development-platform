"""The bearer tokens that bind a repository intelligence request to one tenant.

A tenant's activity worker sends its token as REPO_INTELLIGENCE_TOKEN. Repository intelligence
holds every tenant's accepted tokens and answers a request only for the repositories of the
tenant whose token it carries. In AWS it reads the current and the previous version of each
tenant's token secret from Secrets Manager, so a worker that has not restarted since a rotation
keeps its access. On a workstation it reads REPO_INTELLIGENCE_TOKEN_<TENANT> or derives the
token from REPO_INTELLIGENCE_TOKEN_KEY.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from collections.abc import Iterable, Mapping, Sequence

ENV_PREFIX = "REPO_INTELLIGENCE_TOKEN_"
_DERIVATION_LABEL = "repo-intelligence:"

AcceptedTokens = Mapping[str, Sequence[str]]


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


def single_tokens(tokens: Mapping[str, str]) -> dict[str, tuple[str, ...]]:
    """Return each tenant's one token as the only token the tenant may present."""
    return {tenant: (token,) for tenant, token in tokens.items()}


def tenant_for_token(tokens: AcceptedTokens, presented: str) -> str | None:
    """Return the tenant one of whose tokens was presented.

    Every accepted token of every tenant is compared in constant time, so the time taken does
    not reveal which tenant or which token version matched.
    """
    presented_bytes = presented.encode()
    match = None
    for tenant, accepted in tokens.items():
        if isinstance(accepted, str):
            raise TypeError(f"the accepted tokens of tenant {tenant} must be a sequence of tokens")
        for token in accepted:
            if hmac.compare_digest(token.encode(), presented_bytes):
                match = tenant
    return match
