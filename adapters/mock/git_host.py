"""In-memory git host backend for hermetic tests and local runs."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace

from adapters.interfaces.git_host import (
    BranchRef,
    CodeHit,
    CommitRequest,
    CommitResult,
    FileContent,
    PRComment,
    PRFeedback,
    PublishResult,
    PullRequest,
    publish_from_pull_request,
    reuse_from_pull_request,
)

_UNIT_SEPARATOR = "\x1f"


def _sha(*parts: str) -> str:
    return hashlib.sha256(_UNIT_SEPARATOR.join(parts).encode()).hexdigest()[:40]


@dataclass
class _Repo:
    default_branch: str = "main"
    heads: dict[str, str] = field(default_factory=dict)
    trees: dict[str, dict[str, str]] = field(default_factory=dict)
    applied: dict[str, set[str]] = field(default_factory=dict)
    prs: list[PullRequest] = field(default_factory=list)
    comments: dict[int, list[PRComment]] = field(default_factory=dict)


class MockGitHost:
    def __init__(self) -> None:
        self._repos: dict[str, _Repo] = {}

    def seed_repo(
        self,
        repo: str,
        *,
        default_branch: str = "main",
        files: dict[str, str] | None = None,
    ) -> None:
        tree = dict(files or {})
        record = _Repo(default_branch=default_branch)
        record.trees[default_branch] = tree
        record.heads[default_branch] = _sha(repo, default_branch, *sorted(tree))
        self._repos[repo] = record

    def _repo(self, repo: str) -> _Repo:
        if repo not in self._repos:
            self._repos[repo] = _Repo()
            self._repos[repo].trees["main"] = {}
            self._repos[repo].heads["main"] = _sha(repo, "main")
        return self._repos[repo]

    def source_location(self, repo: str) -> str:
        return f"mock://{repo}"

    async def read_file(self, repo: str, path: str, ref: str) -> FileContent:
        tree = self._repo(repo).trees.get(ref, {})
        if path not in tree:
            raise FileNotFoundError(f"{repo}:{ref}:{path}")
        return FileContent(path=path, text=tree[path], ref=ref)

    async def search_code(self, repo: str, query: str, ref: str = "") -> list[CodeHit]:
        record = self._repo(repo)
        branch = ref or record.default_branch
        hits: list[CodeHit] = []
        for path, text in record.trees.get(branch, {}).items():
            for index, line in enumerate(text.splitlines(), start=1):
                if query in line:
                    hits.append(CodeHit(path=path, line=index, text=line))
        return hits

    async def ensure_branch(self, repo: str, name: str, base: str) -> BranchRef:
        record = self._repo(repo)
        if name in record.heads:
            return BranchRef(repo=repo, name=name, head_sha=record.heads[name], created=False)
        base_head = record.heads.get(base)
        if base_head is None:
            raise KeyError(f"base branch missing: {repo}:{base}")
        record.trees[name] = dict(record.trees.get(base, {}))
        head = _sha(base_head, name)
        record.heads[name] = head
        return BranchRef(repo=repo, name=name, head_sha=head, created=True)

    async def commit_and_push(self, request: CommitRequest) -> CommitResult:
        record = self._repo(request.repo)
        if request.branch not in record.heads:
            await self.ensure_branch(request.repo, request.branch, request.base)
        applied = record.applied.setdefault(request.branch, set())
        if request.execution_id in applied:
            return CommitResult(
                repo=request.repo,
                branch=request.branch,
                commit_sha=record.heads[request.branch],
                pushed=False,
                replayed=True,
            )
        tree = record.trees.setdefault(request.branch, {})
        tree.update(request.files)
        head = _sha(record.heads[request.branch], request.execution_id, request.message)
        record.heads[request.branch] = head
        applied.add(request.execution_id)
        return CommitResult(
            repo=request.repo,
            branch=request.branch,
            commit_sha=head,
            pushed=True,
            replayed=False,
        )

    async def open_draft_pr(
        self, repo: str, head: str, base: str, title: str, body: str
    ) -> PullRequest:
        record = self._repo(repo)
        for index, existing in enumerate(record.prs):
            if existing.head == head and existing.state == "open":
                updated = PullRequest(
                    repo=repo,
                    number=existing.number,
                    url=existing.url,
                    head=head,
                    base=base,
                    title=title,
                    body=body,
                    draft=existing.draft,
                    state="open",
                    created=False,
                )
                record.prs[index] = updated
                return updated
        number = len(record.prs) + 1
        pull = PullRequest(
            repo=repo,
            number=number,
            url=f"https://git.invalid/{repo}/pull/{number}",
            head=head,
            base=base,
            title=title,
            body=body,
            draft=True,
            state="open",
            created=True,
        )
        record.prs.append(pull)
        return pull

    async def find_open_pr(self, repo: str, head: str) -> PullRequest | None:
        record = self._repo(repo)
        for existing in record.prs:
            if existing.head == head and existing.state == "open":
                return existing
        return None

    async def publish(
        self, repo: str, head: str, base: str, title: str, body: str
    ) -> PublishResult:
        pr = await self.open_draft_pr(repo, head, base, title, body)
        return publish_from_pull_request(repo, head, pr)

    async def find_published_branch(self, repo: str, head: str) -> PublishResult | None:
        return reuse_from_pull_request(repo, head, await self.find_open_pr(repo, head))

    async def read_pr_feedback(self, repo: str, number: int) -> PRFeedback:
        record = self._repo(repo)
        pull = next((p for p in record.prs if p.number == number), None)
        if pull is None:
            raise KeyError(f"pull request missing: {repo}#{number}")
        comments = tuple(record.comments.get(number, ()))
        return PRFeedback(repo=repo, number=number, state=pull.state, comments=comments)

    async def delete_branch(self, repo: str, name: str) -> bool:
        record = self._repo(repo)
        if name not in record.heads:
            return False
        record.heads.pop(name, None)
        record.trees.pop(name, None)
        return True

    async def close_pr(self, repo: str, number: int) -> PullRequest:
        record = self._repo(repo)
        for index, existing in enumerate(record.prs):
            if existing.number == number:
                closed = replace(existing, state="closed")
                record.prs[index] = closed
                return closed
        raise KeyError(f"pull request missing: {repo}#{number}")

    async def mark_pr_ready(self, repo: str, number: int) -> PullRequest:
        record = self._repo(repo)
        for index, existing in enumerate(record.prs):
            if existing.number == number:
                ready = replace(existing, draft=False)
                record.prs[index] = ready
                return ready
        raise KeyError(f"pull request missing: {repo}#{number}")

    def add_pr_comment(self, repo: str, number: int, author: str, body: str) -> None:
        self._repo(repo).comments.setdefault(number, []).append(PRComment(author, body))
