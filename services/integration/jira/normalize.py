"""Vendor-neutral normalization of a raw Jira issue into an immutable task shape."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from services.integration.jira._engine import ensure_engine_on_path


def _normalize_rich_text(body: Any) -> str:
    ensure_engine_on_path()
    from rebar_reconciler.inbound_fields import normalize_rich_text

    return normalize_rich_text(body)


@dataclass(frozen=True)
class NormalizedTask:
    task_id: str
    project: str
    summary: str
    description: str
    issue_type: str
    status: str
    priority: str
    labels: tuple[str, ...]
    acceptance_criteria: str
    comments: tuple[str, ...]
    components: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "project": self.project,
            "summary": self.summary,
            "description": self.description,
            "issue_type": self.issue_type,
            "status": self.status,
            "priority": self.priority,
            "labels": list(self.labels),
            "acceptance_criteria": self.acceptance_criteria,
            "comments": list(self.comments),
            "components": list(self.components),
        }


def _name_of(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or value.get("value") or "")
    return str(value or "")


def _project_key(raw: dict[str, Any], fields: dict[str, Any], task_id: str) -> str:
    project = fields.get("project")
    if isinstance(project, dict) and project.get("key"):
        return str(project["key"])
    return task_id.split("-", 1)[0] if "-" in task_id else task_id


def normalize_issue(
    raw: dict[str, Any],
    *,
    raw_comments: Iterable[Any] = (),
    acceptance_field: str = "",
) -> NormalizedTask:
    fields = raw.get("fields") or {}
    task_id = str(raw.get("key") or "")
    labels = tuple(str(label) for label in (fields.get("labels") or []))
    components = tuple(_name_of(component) for component in (fields.get("components") or []))
    acceptance = ""
    if acceptance_field and fields.get(acceptance_field) is not None:
        acceptance = _normalize_rich_text(fields.get(acceptance_field))
    return NormalizedTask(
        task_id=task_id,
        project=_project_key(raw, fields, task_id),
        summary=str(fields.get("summary") or ""),
        description=_normalize_rich_text(fields.get("description")),
        issue_type=_name_of(fields.get("issuetype")),
        status=_name_of(fields.get("status")),
        priority=_name_of(fields.get("priority")),
        labels=labels,
        acceptance_criteria=acceptance,
        comments=tuple(_normalize_rich_text(body) for body in raw_comments),
        components=tuple(name for name in components if name),
    )
