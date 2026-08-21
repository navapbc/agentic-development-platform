"""Accuracy and completeness metrics for a served context package.

These reduce a context package into a small set of numbers suitable for span
attributes, so a served package's per-relationship confidence, its category
completeness, and its relationship counts are readable per execution from the
trace pipeline.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

_RELATIONSHIP_KEYS: tuple[str, ...] = (
    "symbols",
    "references",
    "callers",
    "callees",
    "implementations",
    "related_tests",
    "dependencies",
)


def _confidence(entry: Any) -> float | None:
    if not isinstance(entry, Mapping):
        return None
    provenance = entry.get("provenance")
    if not isinstance(provenance, Mapping):
        return None
    value = provenance.get("confidence")
    if isinstance(value, int | float):
        return float(value)
    return None


def _entries(package: Mapping[str, Any], key: str) -> Sequence[Any]:
    value = package.get(key)
    return value if isinstance(value, Sequence) and not isinstance(value, str | bytes) else ()


def context_accuracy_attributes(package: Mapping[str, Any]) -> dict[str, Any]:
    """Compute per-relationship counts, mean confidence, and category completeness."""
    counts: dict[str, int] = {}
    confidences: list[float] = []
    populated = 0
    for key in _RELATIONSHIP_KEYS:
        entries = _entries(package, key)
        counts[key] = len(entries)
        if entries:
            populated += 1
        for entry in entries:
            confidence = _confidence(entry)
            if confidence is not None:
                confidences.append(confidence)

    total_relationships = sum(counts.values())
    mean_confidence = sum(confidences) / len(confidences) if confidences else 0.0
    completeness = populated / len(_RELATIONSHIP_KEYS)

    attributes: dict[str, Any] = {
        "context.relationship_total": total_relationships,
        "context.mean_confidence": round(mean_confidence, 4),
        "context.completeness": round(completeness, 4),
        "context.scored_relationships": len(confidences),
    }
    for key, value in counts.items():
        attributes[f"context.count.{key}"] = value
    return attributes
