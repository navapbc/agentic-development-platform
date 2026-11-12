from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

BASE_LABEL_PREFIX = "base:"
TRACKER_LABEL_PREFIX = "jira-"


def unresolvable_base_reason(branch: str, repo: str) -> str:
    return f"declared base branch {branch!r} does not exist on {repo}"


class UnresolvableBaseBranch(Exception):
    """A declared base branch that does not resolve on the remote."""

    def __init__(self, branch: str, repo: str) -> None:
        self.branch = branch
        self.repo = repo
        super().__init__(unresolvable_base_reason(branch, repo))


@dataclass(frozen=True)
class BaseDeclaration:
    branch: str = ""
    conflict: str = ""

    @property
    def declared(self) -> bool:
        return bool(self.branch)


def declared_base(labels: Iterable[str]) -> BaseDeclaration:
    """Read the base branch a ticket declares through its labels.

    Conflicts are returned rather than raised because the only caller runs
    outside an exception handler, where a raise would abort the whole pass.
    """
    branches = []
    for label in labels:
        text = str(label)
        if not text.startswith(BASE_LABEL_PREFIX):
            continue
        branch = text[len(BASE_LABEL_PREFIX) :].strip()
        if branch and branch not in branches:
            branches.append(branch)
    if len(branches) > 1:
        listed = ", ".join(sorted(branches))
        return BaseDeclaration(conflict=f"conflicting base branch labels: {listed}")
    return BaseDeclaration(branch=branches[0] if branches else "")


def declared_issue_key(labels: Iterable[str]) -> str:
    """Read the tracker issue key a ticket declares through its labels.

    The ticket store's own identifier means nothing to the tracker, so a pull request that quotes
    it links nowhere. A `jira-<project>-<number>` label carries the key the tracker does know, and
    it stays separate from the store identifier the ledger and the linkback are keyed on.
    """
    for label in labels:
        text = str(label).strip()
        if not text.lower().startswith(TRACKER_LABEL_PREFIX):
            continue
        project, _, number = text[len(TRACKER_LABEL_PREFIX) :].rpartition("-")
        if project and number.isdigit():
            return f"{project.upper()}-{number}"
    return ""
