"""A GitHost that sends each call to the backend its repository is configured with."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from adapters.interfaces.git_host import (
    BranchRef,
    CodeHit,
    CommitRequest,
    CommitResult,
    FileContent,
    GitHost,
    PRFeedback,
    PublishResult,
    PullRequest,
)


class RoutingGitHost:
    """Dispatch every GitHost call by its repository's backend.

    Each backend is built on first use, so an environment whose repositories all use one
    backend never needs the other's configuration.
    """

    def __init__(
        self,
        backend_for: Callable[[str], str],
        factories: Mapping[str, Callable[[], GitHost]],
    ) -> None:
        self._backend_for = backend_for
        self._factories = factories
        self._hosts: dict[str, GitHost] = {}

    def host_for(self, repo: str) -> GitHost:
        backend = self._backend_for(repo)
        host = self._hosts.get(backend)
        if host is None:
            host = self._factories[backend]()
            self._hosts[backend] = host
        return host

    async def read_file(self, repo: str, path: str, ref: str) -> FileContent:
        return await self.host_for(repo).read_file(repo, path, ref)

    async def search_code(self, repo: str, query: str, ref: str = "") -> list[CodeHit]:
        return await self.host_for(repo).search_code(repo, query, ref)

    def source_location(self, repo: str) -> str:
        return self.host_for(repo).source_location(repo)

    async def ensure_branch(self, repo: str, name: str, base: str) -> BranchRef:
        return await self.host_for(repo).ensure_branch(repo, name, base)

    async def commit_and_push(self, request: CommitRequest) -> CommitResult:
        return await self.host_for(request.repo).commit_and_push(request)

    async def open_draft_pr(
        self, repo: str, head: str, base: str, title: str, body: str
    ) -> PullRequest:
        return await self.host_for(repo).open_draft_pr(repo, head, base, title, body)

    async def find_open_pr(self, repo: str, head: str) -> PullRequest | None:
        return await self.host_for(repo).find_open_pr(repo, head)

    async def publish(
        self, repo: str, head: str, base: str, title: str, body: str
    ) -> PublishResult:
        return await self.host_for(repo).publish(repo, head, base, title, body)

    async def find_published_branch(self, repo: str, head: str) -> PublishResult | None:
        return await self.host_for(repo).find_published_branch(repo, head)

    async def read_pr_feedback(self, repo: str, number: int) -> PRFeedback:
        return await self.host_for(repo).read_pr_feedback(repo, number)

    async def delete_branch(self, repo: str, name: str) -> bool:
        return await self.host_for(repo).delete_branch(repo, name)

    async def close_pr(self, repo: str, number: int) -> PullRequest:
        return await self.host_for(repo).close_pr(repo, number)

    async def mark_pr_ready(self, repo: str, number: int) -> PullRequest:
        return await self.host_for(repo).mark_pr_ready(repo, number)
