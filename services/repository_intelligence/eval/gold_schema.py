"""Gold-set record schema and JSONL serialization."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class QueryType(StrEnum):
    SYMBOL_DEFINITION = "symbol_definition"
    REFERENCES = "references"
    CALLERS = "callers"
    IMPLEMENTATIONS = "implementations"
    MODULE_DEPENDENCIES = "module_dependencies"
    RELATED_TESTS = "related_tests"


@dataclass(frozen=True)
class ExpectedItem:
    path: str | None = None
    line: int | None = None
    col: int | None = None
    symbol: str | None = None
    module: str | None = None
    is_external: bool | None = None
    test_id: str | None = None
    module_level: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass(frozen=True)
class GoldRecord:
    repo: str
    commit: str
    query_type: QueryType
    input: dict[str, Any]
    expected: tuple[ExpectedItem, ...]
    oracle: str
    notes: str | None = None

    def to_dict(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "record_type": "gold",
            "repo": self.repo,
            "commit": self.commit,
            "query_type": str(self.query_type),
            "input": self.input,
            "expected": [item.to_dict() for item in self.expected],
            "oracle": self.oracle,
        }
        if self.notes is not None:
            record["notes"] = self.notes
        return record


@dataclass(frozen=True)
class GoldMeta:
    repo: str
    commit: str
    generated_at: str
    oracles: dict[str, str]
    toolchain: dict[str, str] = field(default_factory=dict)
    notes: str | None = None

    def to_dict(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "record_type": "meta",
            "repo": self.repo,
            "commit": self.commit,
            "generated_at": self.generated_at,
            "oracles": self.oracles,
            "toolchain": self.toolchain,
        }
        if self.notes is not None:
            record["notes"] = self.notes
        return record


def write_gold_set(path: str | Path, meta: GoldMeta, records: list[GoldRecord]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(meta.to_dict(), sort_keys=True))
        handle.write("\n")
        for record in records:
            handle.write(json.dumps(record.to_dict(), sort_keys=True))
            handle.write("\n")


def read_gold_set(path: str | Path) -> tuple[GoldMeta | None, list[GoldRecord]]:
    meta: GoldMeta | None = None
    records: list[GoldRecord] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        payload = json.loads(line)
        if payload.get("record_type") == "meta":
            meta = GoldMeta(
                repo=payload["repo"],
                commit=payload["commit"],
                generated_at=payload["generated_at"],
                oracles=payload["oracles"],
                toolchain=payload.get("toolchain", {}),
                notes=payload.get("notes"),
            )
            continue
        records.append(
            GoldRecord(
                repo=payload["repo"],
                commit=payload["commit"],
                query_type=QueryType(payload["query_type"]),
                input=payload["input"],
                expected=tuple(ExpectedItem(**item) for item in payload["expected"]),
                oracle=payload["oracle"],
                notes=payload.get("notes"),
            )
        )
    return meta, records
