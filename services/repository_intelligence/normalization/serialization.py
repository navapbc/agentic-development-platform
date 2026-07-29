"""Serialization and prompt rendering for repository-intelligence context packages."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from enum import Enum
from typing import Any

from services.repository_intelligence.normalization.models import ContextPackage

_MAX_ITEMS = 20


def _jsonify(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _jsonify(item) for key, item in value.items()}
    if isinstance(value, (list | tuple)):
        return [_jsonify(item) for item in value]
    return value


def context_package_to_dict(package: ContextPackage) -> dict[str, Any]:
    """Serialize a context package into a JSON-native dictionary."""
    return _jsonify(asdict(package))


def _location(entry: Mapping[str, Any]) -> str:
    location = entry.get("location") or {}
    path = location.get("path", "")
    line = location.get("start_line")
    return f"{path}:{line}" if line else path


def render_context_summary(package: Mapping[str, Any]) -> str:
    """Render a compact, deterministic text block for the planning prompt."""
    lines: list[str] = []
    repository = package.get("repository") or "unknown"
    commit = package.get("indexed_commit") or "unindexed"
    lines.append(f"Repository: {repository} @ {commit}")
    lines.append(f"Focus: {package.get('focus', '')}")

    symbols = package.get("symbols") or []
    if symbols:
        lines.append("Symbols:")
        for symbol in symbols[:_MAX_ITEMS]:
            kind = symbol.get("kind", "")
            lines.append(f"  - {symbol.get('name', '')} ({kind}) {_location(symbol)}")

    candidate_files = package.get("candidate_files") or []
    if candidate_files:
        lines.append("Candidate files:")
        lines.extend(f"  - {path}" for path in candidate_files[:_MAX_ITEMS])

    for label, key in (("Callers", "callers"), ("Callees", "callees")):
        edges = package.get(key) or []
        if edges:
            lines.append(f"{label}:")
            for edge in edges[:_MAX_ITEMS]:
                caller = edge.get("caller") or {}
                callee = edge.get("callee") or {}
                lines.append(f"  - {caller.get('name', '')} -> {callee.get('name', '')}")

    tests = package.get("related_tests") or []
    if tests:
        lines.append("Related tests:")
        lines.extend(
            f"  - {test.get('test_id', '')} {_location(test)}" for test in tests[:_MAX_ITEMS]
        )

    dependencies = package.get("dependencies") or []
    if dependencies:
        lines.append("Module dependencies:")
        for edge in dependencies[:_MAX_ITEMS]:
            lines.append(f"  - {edge.get('to_module', '')} ({edge.get('kind', '')})")

    build_targets = package.get("build_targets") or []
    if build_targets:
        lines.append("Build targets: " + ", ".join(build_targets[:_MAX_ITEMS]))

    config_files = package.get("config_files") or []
    if config_files:
        lines.append("Config files: " + ", ".join(config_files[:_MAX_ITEMS]))

    recent_changes = package.get("recent_changes") or []
    if recent_changes:
        lines.append("Recent related changes:")
        for change in recent_changes[:_MAX_ITEMS]:
            sha = (change.get("commit_sha") or "")[:7]
            lines.append(f"  - {sha} {change.get('summary', '')} ({change.get('path', '')})")

    return "\n".join(lines)
