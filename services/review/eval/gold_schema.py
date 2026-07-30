"""Gold-set schema and JSONL serialization for the review-gate eval harness."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from services.review.types import ReviewStage

VERDICTS = ("pass", "block")


@dataclass(frozen=True)
class ReviewCase:
    stage: ReviewStage
    case_id: str
    subject: str
    expected_verdict: str
    expected_findings: tuple[str, ...] = ()
    notes: str | None = None

    def to_dict(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "record_type": "case",
            "stage": self.stage.value,
            "case_id": self.case_id,
            "subject": self.subject,
            "expected_verdict": self.expected_verdict,
            "expected_findings": list(self.expected_findings),
        }
        if self.notes is not None:
            record["notes"] = self.notes
        return record


@dataclass(frozen=True)
class GoldMeta:
    generated_at: str
    baseline: str
    notes: str | None = None

    def to_dict(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "record_type": "meta",
            "generated_at": self.generated_at,
            "baseline": self.baseline,
        }
        if self.notes is not None:
            record["notes"] = self.notes
        return record


def write_gold_set(path: str | Path, meta: GoldMeta, cases: list[ReviewCase]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(meta.to_dict(), sort_keys=True))
        handle.write("\n")
        for case in cases:
            handle.write(json.dumps(case.to_dict(), sort_keys=True))
            handle.write("\n")


def read_gold_set(path: str | Path) -> tuple[GoldMeta | None, list[ReviewCase]]:
    meta: GoldMeta | None = None
    cases: list[ReviewCase] = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        payload = json.loads(line)
        if payload.get("record_type") == "meta":
            meta = GoldMeta(
                generated_at=payload["generated_at"],
                baseline=payload["baseline"],
                notes=payload.get("notes"),
            )
            continue
        verdict = payload["expected_verdict"]
        if verdict not in VERDICTS:
            raise ValueError(f"case {payload['case_id']} has invalid verdict {verdict}")
        cases.append(
            ReviewCase(
                stage=ReviewStage(payload["stage"]),
                case_id=payload["case_id"],
                subject=payload["subject"],
                expected_verdict=verdict,
                expected_findings=tuple(payload.get("expected_findings", [])),
                notes=payload.get("notes"),
            )
        )
    return meta, cases
