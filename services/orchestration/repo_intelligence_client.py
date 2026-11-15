"""Async HTTP client for the resident Repository Intelligence Service."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
from scripts.config import get_settings

from services.integration.credentials import ConfigError
from services.repository_intelligence.tenant_tokens import derive_token

if TYPE_CHECKING:
    from scripts.config import Settings


def repo_intelligence_token(settings: Settings) -> str:
    """Return this process's bearer token for repository intelligence.

    REPO_INTELLIGENCE_TOKEN wins. A workstation that sets REPO_INTELLIGENCE_TOKEN_KEY and TENANT
    derives the token of its tenant from the key.
    """
    token = settings.repo_intelligence_token.strip()
    if token:
        return token
    if settings.repo_intelligence_token_key and settings.tenant:
        return derive_token(settings.repo_intelligence_token_key, settings.tenant)
    raise ConfigError(
        "REPO_INTELLIGENCE_TOKEN is not set, and repository intelligence refuses a request "
        "without the token of its tenant. Set REPO_INTELLIGENCE_TOKEN, or set "
        "REPO_INTELLIGENCE_TOKEN_KEY and TENANT on a workstation."
    )


def repo_intelligence_headers(settings: Settings) -> dict[str, str]:
    return {"Authorization": f"Bearer {repo_intelligence_token(settings)}"}


async def fetch_context_package(
    repo: str, commit: str, focus: str, detail: str = ""
) -> dict[str, Any]:
    """Fetch a serialized context package from the repository-intelligence service."""
    settings = get_settings()
    headers = repo_intelligence_headers(settings)
    payload = {"repo": repo, "commit": commit, "focus": focus, "detail": detail}
    async with httpx.AsyncClient(base_url=settings.repo_intelligence_url, timeout=240.0) as client:
        resp = await client.post("/context_package", json=payload, headers=headers)
        resp.raise_for_status()
        return resp.json()
