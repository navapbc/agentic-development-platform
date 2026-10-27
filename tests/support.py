"""Shared test helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from scripts.config import Settings
from services.review.evidence import EvidenceRecord


def hermetic_settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # pyright: ignore[reportCallIssue]


def capture_evidence(monkeypatch: pytest.MonkeyPatch) -> list[EvidenceRecord]:
    """Swap the Postgres evidence sink for a recorder and return the records it collects.

    ``build_evidence_sink`` resolves ``PostgresEvidenceSink`` by name at call time, so
    patching the attribute reaches every producer without touching the factory.
    """
    records: list[EvidenceRecord] = []

    class _RecordingSink:
        async def persist(self, record: EvidenceRecord) -> str:
            records.append(record)
            return record.artifact_id

    monkeypatch.setattr("services.review.evidence.PostgresEvidenceSink", _RecordingSink)
    return records


def seed_rebar_store(root: Path, tickets: list[dict] | None = None) -> list[str]:
    """Init a local rebar store with a reader identity and a few tickets for tests.

    Kept independent of the xlsx importer so tests do not depend on any export.
    """
    import rebar

    root.mkdir(parents=True, exist_ok=True)
    if not (root / ".git").exists():
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "agent-platform@users.noreply.invalid"],
            cwd=root,
            check=True,
        )
        subprocess.run(["git", "config", "user.name", "agent-platform"], cwd=root, check=True)
    rebar.init_repo(repo_root=str(root), force_new_store=True)
    if not rebar.resolve_current_identity(repo_root=str(root)):
        identity_id = rebar.create_identity(
            "agent-platform", "agent-platform@users.noreply.invalid", repo_root=str(root)
        )
        rebar.use_identity(str(identity_id), repo_root=str(root))

    specs = tickets if tickets is not None else _DEFAULT_TICKETS
    created: list[str] = []
    for spec in specs:
        created.append(
            str(
                rebar.create_ticket(
                    spec.get("ticket_type", "task"),
                    spec["title"],
                    description=spec.get("description"),
                    priority=spec.get("priority", 2),
                    repos=spec.get("repos"),
                    tags=spec.get("tags"),
                    repo_root=str(root),
                )
            )
        )
    return created


_DEFAULT_TICKETS: list[dict] = [
    {
        "ticket_type": "task",
        "title": "Add pagination to the task list endpoint",
        "description": "Add limit and offset to GET /tasks.",
        "priority": 2,
        "repos": ["agentic-demo-service"],
        "tags": ["enhancement", "api"],
    },
    {
        "ticket_type": "task",
        "title": "Add due-date filtering to task queries",
        "description": "Add due_before and due_after filters.",
        "priority": 3,
        "repos": ["agentic-demo-service"],
        "tags": ["enhancement", "api"],
    },
]
