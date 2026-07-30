"""Platform Jira adapter over rebar's vendor Cloud and Data Center clients."""

from services.integration.jira.adapter import (
    CLOUD,
    DATACENTER,
    JiraAdapter,
    build_jira_adapter,
)
from services.integration.jira.normalize import NormalizedTask, normalize_issue
from services.integration.jira.snapshot import (
    AllowlistError,
    PostgresTaskSnapshotStore,
    SnapshotCandidate,
    TaskSnapshot,
    TaskSnapshotStore,
    capture_task_snapshot,
    content_hash,
)

__all__ = [
    "CLOUD",
    "DATACENTER",
    "AllowlistError",
    "JiraAdapter",
    "NormalizedTask",
    "PostgresTaskSnapshotStore",
    "SnapshotCandidate",
    "TaskSnapshot",
    "TaskSnapshotStore",
    "build_jira_adapter",
    "capture_task_snapshot",
    "content_hash",
    "normalize_issue",
]
