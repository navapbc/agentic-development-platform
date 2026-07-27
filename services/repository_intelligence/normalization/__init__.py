"""Normalized result shapes shared by all repository-intelligence providers."""

from __future__ import annotations

from services.repository_intelligence.normalization.models import (
    NON_AUTHORITATIVE_SOURCES,
    CallEdge,
    ChangeImpact,
    ContextPackage,
    DependencyEdge,
    IndexStatus,
    Location,
    Provenance,
    Reference,
    RelatedTest,
    RepoRef,
    SearchHit,
    Source,
    Symbol,
    SymbolKind,
)

__all__ = [
    "NON_AUTHORITATIVE_SOURCES",
    "CallEdge",
    "ChangeImpact",
    "ContextPackage",
    "DependencyEdge",
    "IndexStatus",
    "Location",
    "Provenance",
    "Reference",
    "RelatedTest",
    "RepoRef",
    "SearchHit",
    "Source",
    "Symbol",
    "SymbolKind",
]
