"""Warm registry that builds and caches one RIS per registered tenant repository."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from services.repository_intelligence.context_builder.context_service import (
    RepositoryIntelligenceService,
)
from services.repository_intelligence.indexing.reindex import IndexRoots
from services.repository_intelligence.languages import Language, primary_language

RepoKey = tuple[str, str]


@dataclass(frozen=True)
class RepoSpec:
    name: str
    root: Path
    commit: str | None = None
    cbm_binary: str | None = None
    tenant: str = ""
    index_roots: IndexRoots | None = None

    @property
    def key(self) -> RepoKey:
        return (self.tenant, self.name)


_Builder = Callable[..., RepositoryIntelligenceService]


class RepositoryIntelligenceRegistry:
    """Route context requests to a per-repository service, building each one lazily.

    Services are keyed by tenant and repository, so two tenants that name a repository the same
    receive separate services over separate checkouts and indexes.
    """

    def __init__(
        self,
        specs: Iterable[RepoSpec],
        *,
        builder: _Builder = RepositoryIntelligenceService.from_repo,
    ) -> None:
        self._specs = {spec.key: spec for spec in specs}
        self._services: dict[RepoKey, RepositoryIntelligenceService] = {}
        self._languages: dict[RepoKey, Language] = {}
        self._builder = builder
        self._lock = threading.Lock()

    def repos(self) -> set[RepoKey]:
        return set(self._specs)

    def replace_specs(self, specs: Iterable[RepoSpec]) -> None:
        """Serve these specs, keeping the cached service of every spec that did not change."""
        replacement = {spec.key: spec for spec in specs}
        with self._lock:
            for key in list(self._services):
                if replacement.get(key) != self._specs.get(key):
                    self._services.pop(key, None)
                    self._languages.pop(key, None)
            self._specs = replacement

    def service(self, repo: str, tenant: str = "") -> RepositoryIntelligenceService | None:
        key = (tenant, repo)
        with self._lock:
            spec = self._specs.get(key)
        if spec is None:
            return None
        detected = primary_language(spec.root)
        with self._lock:
            cached = self._services.get(key)
            if cached is not None and self._languages.get(key) == detected:
                return cached
        options: dict[str, object] = {"commit": spec.commit, "cbm_binary": spec.cbm_binary}
        if spec.index_roots is not None:
            options["index_roots"] = spec.index_roots
        service = self._builder(spec.root, **options)
        with self._lock:
            self._services[key] = service
            self._languages[key] = detected
        return service
