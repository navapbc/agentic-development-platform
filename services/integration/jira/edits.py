"""Classify post-start Jira edits and pause the execution on material changes."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Protocol

from database.models.schema import audit_events
from scripts.config import Settings, get_settings
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from services.integration.jira.credentials import acceptance_field
from services.integration.jira.normalize import normalize_issue

UNCHANGED = "unchanged"
METADATA_ONLY = "metadata_only"
NON_MATERIAL = "non_material"
MATERIAL_CHANGE = "material_change"
EXECUTION_INVALIDATING = "execution_invalidating"

_MATERIAL_FIELDS = ("summary", "description", "acceptance_criteria")
_IDENTITY_FIELDS = ("task_id", "project", "issue_type")
_INVALIDATING_STATUSES = frozenset(
    {"cancelled", "canceled", "rejected", "abandoned", "won't do", "wont do"}
)


class PauseSignal(Exception):
    def __init__(self, classification: EditClassification) -> None:
        self.classification = classification
        super().__init__(classification.reason)


@dataclass(frozen=True)
class EditClassification:
    category: str
    changed_fields: tuple[str, ...]
    requires_pause: bool
    reason: str


@dataclass(frozen=True)
class PauseRecord:
    execution_id: str
    task_id: str
    category: str
    changed_fields: tuple[str, ...]
    reason: str


class EditPauseStore(Protocol):
    async def record(self, record: PauseRecord) -> None: ...


def _changed_fields(stored: dict[str, Any], fresh: dict[str, Any]) -> tuple[str, ...]:
    keys = set(stored) | set(fresh)
    return tuple(sorted(key for key in keys if stored.get(key) != fresh.get(key)))


def _is_invalidating(fresh: dict[str, Any], changed: tuple[str, ...]) -> bool:
    if any(field in changed for field in _IDENTITY_FIELDS):
        return True
    if "status" in changed:
        return str(fresh.get("status") or "").strip().lower() in _INVALIDATING_STATUSES
    return False


def classify_edit(stored: dict[str, Any], fresh: dict[str, Any]) -> EditClassification:
    changed = _changed_fields(stored, fresh)
    if not changed:
        return EditClassification(UNCHANGED, changed, False, "no changes detected")
    if _is_invalidating(fresh, changed):
        return EditClassification(
            EXECUTION_INVALIDATING, changed, True, f"execution-invalidating edit: {changed}"
        )
    material = tuple(field for field in changed if field in _MATERIAL_FIELDS)
    if material:
        return EditClassification(
            MATERIAL_CHANGE, changed, True, f"material requirements change: {material}"
        )
    if changed == ("comments",):
        return EditClassification(NON_MATERIAL, changed, False, "non-material clarification")
    return EditClassification(METADATA_ONLY, changed, False, f"metadata-only edit: {changed}")


def _pause_event_id(execution_id: str, category: str, changed_fields: tuple[str, ...]) -> str:
    key = f"execution_pause:{execution_id}:{category}:{','.join(changed_fields)}"
    return hashlib.sha256(key.encode()).hexdigest()


class PostgresEditPauseStore:
    def __init__(self, engine: AsyncEngine | None = None) -> None:
        self._engine = engine

    def _get_engine(self) -> AsyncEngine:
        if self._engine is None:
            self._engine = create_async_engine(
                get_settings().database_url, future=True, pool_pre_ping=True
            )
        return self._engine

    async def record(self, record: PauseRecord) -> None:
        engine = self._get_engine()
        async with engine.begin() as conn:
            await conn.execute(
                insert(audit_events)
                .values(
                    event_id=_pause_event_id(
                        record.execution_id, record.category, record.changed_fields
                    ),
                    task_id=record.task_id,
                    execution_id=record.execution_id,
                    event_type="execution_paused",
                    new_state="paused",
                    decision="pause",
                    reason=record.reason,
                    correlation_ids={
                        "category": record.category,
                        "changed_fields": list(record.changed_fields),
                    },
                )
                .on_conflict_do_nothing(index_elements=[audit_events.c.event_id])
            )


async def evaluate_task_edit(
    stored_payload: dict[str, Any],
    fresh_payload: dict[str, Any],
    *,
    execution_id: str,
    task_id: str,
    store: EditPauseStore | None = None,
) -> EditClassification:
    classification = classify_edit(stored_payload, fresh_payload)
    if classification.requires_pause:
        store = store or PostgresEditPauseStore()
        await store.record(
            PauseRecord(
                execution_id=execution_id,
                task_id=task_id,
                category=classification.category,
                changed_fields=classification.changed_fields,
                reason=classification.reason,
            )
        )
        raise PauseSignal(classification)
    return classification


class _IssueReader(Protocol):
    def get_issue(self, issue_key: str) -> dict[str, Any]: ...


async def recheck_issue_before_pr(
    reader: _IssueReader,
    issue_key: str,
    stored_payload: dict[str, Any],
    *,
    execution_id: str,
    task_id: str,
    settings: Settings | None = None,
    store: EditPauseStore | None = None,
    raw_comments: tuple[Any, ...] = (),
) -> EditClassification:
    settings = settings or get_settings()
    fresh = normalize_issue(
        reader.get_issue(issue_key),
        raw_comments=raw_comments,
        acceptance_field=acceptance_field(settings),
    ).to_dict()
    return await evaluate_task_edit(
        stored_payload,
        fresh,
        execution_id=execution_id,
        task_id=task_id,
        store=store,
    )
