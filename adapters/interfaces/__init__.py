"""Contract exports for the vendor-neutral git host adapter."""

from adapters.interfaces.git_host import (
    BranchRef,
    CodeHit,
    CommitRequest,
    CommitResult,
    FileContent,
    GitHost,
    PRComment,
    PRFeedback,
    PullRequest,
    agent_branch_name,
)

__all__ = [
    "BranchRef",
    "CodeHit",
    "CommitRequest",
    "CommitResult",
    "FileContent",
    "GitHost",
    "PRComment",
    "PRFeedback",
    "PullRequest",
    "agent_branch_name",
]
