"""Async HTTP client for the resident Repository Intelligence Service."""

from __future__ import annotations

from typing import Any

import httpx
from scripts.config import get_settings


async def fetch_context_package(repo: str, commit: str, focus: str) -> dict[str, Any]:
    """Fetch a serialized context package from the repository-intelligence service."""
    settings = get_settings()
    payload = {"repo": repo, "commit": commit, "focus": focus}
    async with httpx.AsyncClient(base_url=settings.repo_intelligence_url, timeout=120.0) as client:
        resp = await client.post("/context_package", json=payload)
        resp.raise_for_status()
        return resp.json()
