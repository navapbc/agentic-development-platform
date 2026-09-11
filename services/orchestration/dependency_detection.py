"""Planning-phase cross-ticket dependency detection over file impact and symbols.

The pure logic here has no I/O. It builds a run's predicted touch set from the
repository-intelligence context package and the ticket's declared file impact,
finds the files where the run references a symbol undefined at its base, and
compares both against the published touch sets of the runs still in flight. A
match names the blocking run. The activity in
``services.orchestration.temporal_activities.dependency`` supplies the I/O.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

FILE_OVERLAP = "file_overlap"
UNRESOLVED_SYMBOL = "unresolved_symbol"


@dataclass(frozen=True)
class PeerRun:
    execution_id: str
    task_id: str
    file_impact: tuple[str, ...]


@dataclass(frozen=True)
class DependencyFinding:
    blocking_task_id: str
    blocking_execution_id: str
    reason: str
    overlap: tuple[str, ...] = field(default_factory=tuple)


def _package_files(package: dict[str, Any]) -> set[str]:
    files: set[str] = {str(path) for path in (package.get("candidate_files") or []) if path}
    for symbol in package.get("symbols") or []:
        path = (symbol.get("location") or {}).get("path")
        if path:
            files.add(str(path))
    return files


def predicted_touch_set(package: dict[str, Any], declared: Iterable[str] = ()) -> tuple[str, ...]:
    """Return the files the run is predicted to touch, from context plus declared impact."""
    files = _package_files(package)
    files |= {str(path) for path in declared if path}
    return tuple(sorted(files))


def unresolved_symbol_files(package: dict[str, Any]) -> tuple[str, ...]:
    """Return the files where the run references a symbol undefined at its base."""
    defined = {str(symbol.get("name")) for symbol in package.get("symbols") or []}
    files: set[str] = set()
    for reference in package.get("references") or []:
        if reference.get("is_definition"):
            continue
        name = reference.get("symbol")
        if name and str(name) not in defined:
            path = (reference.get("location") or {}).get("path")
            if path:
                files.add(str(path))
    return tuple(sorted(files))


def detect_dependency(
    *,
    touch_set: Sequence[str],
    symbol_files: Sequence[str],
    peers: Sequence[PeerRun],
) -> DependencyFinding | None:
    """Return the first still-in-flight peer this run depends on, or None.

    File overlap takes precedence over the symbol signal, and peers are examined
    in the order given so the result is deterministic.
    """
    own = set(touch_set)
    symbols = set(symbol_files)
    for peer in peers:
        overlap = own & set(peer.file_impact)
        if overlap:
            return DependencyFinding(
                blocking_task_id=peer.task_id,
                blocking_execution_id=peer.execution_id,
                reason=FILE_OVERLAP,
                overlap=tuple(sorted(overlap)),
            )
    for peer in peers:
        overlap = symbols & set(peer.file_impact)
        if overlap:
            return DependencyFinding(
                blocking_task_id=peer.task_id,
                blocking_execution_id=peer.execution_id,
                reason=UNRESOLVED_SYMBOL,
                overlap=tuple(sorted(overlap)),
            )
    return None
