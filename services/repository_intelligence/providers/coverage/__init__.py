"""Coverage-based related-tests provider."""

from __future__ import annotations

from services.repository_intelligence.providers.coverage.coverage_provider import (
    CoverageError,
    CoverageProvider,
    SymbolSpan,
    build_test_index,
    coverage_map_from_json,
    related_tests_from_coverage,
    resolve_symbol_span,
)

__all__ = [
    "CoverageError",
    "CoverageProvider",
    "SymbolSpan",
    "build_test_index",
    "coverage_map_from_json",
    "related_tests_from_coverage",
    "resolve_symbol_span",
]
