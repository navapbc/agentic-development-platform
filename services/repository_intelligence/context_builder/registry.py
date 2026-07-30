"""Warm registry that builds and caches one RIS per registered repository."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from services.repository_intelligence.context_builder.context_service import (
    RepositoryIntelligenceService,
)
from services.repository_intelligence.languages import Language, primary_language


@dataclass(frozen=True)
class RepoSpec:
    name: str
    root: Path
    commit: str | None = None
    cbm_binary: str | None = None


_Builder = Callable[..., RepositoryIntelligenceService]


class RepositoryIntelligenceRegistry:
    """Route context requests to a per-repository service, building each one lazily."""

    def __init__(
        self,
        specs: list[RepoSpec],
        *,
        builder: _Builder = RepositoryIntelligenceService.from_repo,
    ) -> None:
        self._specs = {spec.name: spec for spec in specs}
        self._services: dict[str, RepositoryIntelligenceService] = {}
        self._languages: dict[str, Language] = {}
        self._builder = builder

    def repos(self) -> set[str]:
        return set(self._specs)

    def service(self, repo: str) -> RepositoryIntelligenceService | None:
        spec = self._specs.get(repo)
        if spec is None:
            return None
        detected = primary_language(spec.root)
        cached = self._services.get(repo)
        if cached is not None and self._languages.get(repo) == detected:
            return cached
        service = self._builder(spec.root, commit=spec.commit, cbm_binary=spec.cbm_binary)
        self._services[repo] = service
        self._languages[repo] = detected
        return service
