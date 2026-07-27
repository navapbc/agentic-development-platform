"""Public API for the repository-intelligence layer: the shared provider Protocol."""

from __future__ import annotations

from services.repository_intelligence.api.provider import (
    RepositoryIntelligenceProvider,
)

__all__ = ["RepositoryIntelligenceProvider"]
