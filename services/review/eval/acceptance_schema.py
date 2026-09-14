"""Labeled acceptance-set schema and JSONL serialization for the acceptance gate.

Each case pairs a ticket's acceptance criteria and the completion evidence a run
produced with a known outcome of ``met`` or ``unmet``. The acceptance harness runs
the acceptance gate over these cases and scores each verdict against the outcome, so
the labels here define the ground truth the gate is measured against.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

OUTCOME_MET = "met"
OUTCOME_UNMET = "unmet"
OUTCOMES = (OUTCOME_MET, OUTCOME_UNMET)


class AcceptanceSetError(Exception):
    """The acceptance set is missing or malformed."""


@dataclass(frozen=True)
class AcceptanceCase:
    case_id: str
    acceptance_criteria: str
    completion_evidence: str
    outcome: str
    notes: str | None = None

    def to_dict(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "record_type": "case",
            "case_id": self.case_id,
            "acceptance_criteria": self.acceptance_criteria,
            "completion_evidence": self.completion_evidence,
            "outcome": self.outcome,
        }
        if self.notes is not None:
            record["notes"] = self.notes
        return record


def _parse_case(record: dict[str, Any]) -> AcceptanceCase:
    for field_name in ("case_id", "acceptance_criteria", "completion_evidence", "outcome"):
        value = record.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise AcceptanceSetError(f"case requires a non-empty string {field_name!r}")
    outcome = record["outcome"]
    if outcome not in OUTCOMES:
        raise AcceptanceSetError(f"case {record['case_id']!r} outcome must be one of {OUTCOMES}")
    notes = record.get("notes")
    return AcceptanceCase(
        case_id=record["case_id"],
        acceptance_criteria=record["acceptance_criteria"],
        completion_evidence=record["completion_evidence"],
        outcome=outcome,
        notes=notes if isinstance(notes, str) else None,
    )


def read_acceptance_set(path: str | Path) -> list[AcceptanceCase]:
    """Read and validate the labeled acceptance set, requiring both outcomes present."""
    resolved = Path(path)
    if not resolved.is_file():
        raise AcceptanceSetError(f"acceptance set not found: {resolved}")
    cases: list[AcceptanceCase] = []
    seen: set[str] = set()
    for line in resolved.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text:
            continue
        record = json.loads(text)
        if record.get("record_type") == "meta":
            continue
        case = _parse_case(record)
        if case.case_id in seen:
            raise AcceptanceSetError(f"duplicate case id {case.case_id!r}")
        seen.add(case.case_id)
        cases.append(case)
    if not cases:
        raise AcceptanceSetError(f"acceptance set is empty: {resolved}")
    outcomes = {case.outcome for case in cases}
    if OUTCOME_MET not in outcomes or OUTCOME_UNMET not in outcomes:
        raise AcceptanceSetError("acceptance set must contain at least one met and one unmet case")
    return cases


def write_acceptance_set(path: str | Path, cases: list[AcceptanceCase]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        for case in cases:
            handle.write(json.dumps(case.to_dict(), sort_keys=True))
            handle.write("\n")
