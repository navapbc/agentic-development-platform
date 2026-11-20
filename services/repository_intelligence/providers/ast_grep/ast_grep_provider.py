"""Structural repository-intelligence provider backed by ast-grep patterns.

Runs ast-grep structural patterns (FastAPI routes, Pydantic models, HTTPException
raise sites, or arbitrary caller-supplied patterns) and normalizes the JSON matches
to SearchHit with authoritative Source.AST_GREP provenance for context-package
enrichment. The provider does not build a persistent index; it scans on demand.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from pathlib import Path

from services import process
from services.repository_intelligence.normalization.models import (
    Location,
    Provenance,
    RepoRef,
    SearchHit,
    Source,
)

DEFAULT_AST_GREP_CMD = "ast-grep"

ROUTE_PATTERN = "@app.$METHOD($$$ARGS)"
RAISE_HTTP_PATTERN = "raise HTTPException($$$ARGS)"
PYDANTIC_MODEL_PATTERN = "class $NAME(BaseModel): $$$BODY"

_AST_GREP_CONFIDENCE = 1.0


class AstGrepError(RuntimeError):
    """Raised when an ast-grep invocation fails."""


@dataclass(frozen=True)
class StructuralMatch:
    text: str
    location: Location
    single: dict[str, str]
    multi: dict[str, tuple[str, ...]]


def matches_from_json(output: str) -> list[StructuralMatch]:
    """Parse `ast-grep run --json` output into deterministic structural matches."""
    text = output.strip()
    if not text:
        return []
    matches: list[StructuralMatch] = []
    for record in json.loads(text):
        span = record["range"]
        start = span["start"]
        end = span["end"]
        meta = record.get("metaVariables") or {}
        single = {name: node.get("text", "") for name, node in (meta.get("single") or {}).items()}
        multi = {
            name: tuple(node.get("text", "") for node in nodes)
            for name, nodes in (meta.get("multi") or {}).items()
        }
        matches.append(
            StructuralMatch(
                text=record.get("text", ""),
                location=Location(
                    path=record.get("file", ""),
                    start_line=int(start["line"]) + 1,
                    start_col=int(start["column"]),
                    end_line=int(end["line"]) + 1,
                    end_col=int(end["column"]),
                ),
                single=single,
                multi=multi,
            )
        )
    matches.sort(key=lambda m: (m.location.path, m.location.start_line, m.location.start_col))
    return matches


def hit_from_match(match: StructuralMatch) -> SearchHit:
    """Project a structural match onto a normalized SearchHit."""
    snippet = match.text.splitlines()[0].strip() if match.text else ""
    return SearchHit(
        location=match.location,
        snippet=snippet,
        score=_AST_GREP_CONFIDENCE,
        provenance=Provenance(source=Source.AST_GREP, confidence=_AST_GREP_CONFIDENCE),
    )


class AstGrepProvider:
    """Runs ast-grep structural patterns and normalizes matches for enrichment."""

    def __init__(
        self,
        repo_root: str | Path,
        *,
        language: str = "python",
        ast_grep_cmd: str = DEFAULT_AST_GREP_CMD,
    ) -> None:
        self._root = Path(repo_root).resolve()
        self._language = language
        self._ast_grep_cmd = ast_grep_cmd

    def run_pattern(self, pattern: str, *, language: str | None = None) -> list[StructuralMatch]:
        output = self._run(pattern, language or self._language)
        return matches_from_json(output)

    def repo_search_structural(
        self, repo: RepoRef, pattern: str, *, language: str | None = None, limit: int = 50
    ) -> list[SearchHit]:
        matches = self.run_pattern(pattern, language=language)
        return [hit_from_match(match) for match in matches[:limit]]

    def find_routes(self) -> list[StructuralMatch]:
        return self.run_pattern(ROUTE_PATTERN)

    def find_raise_sites(self) -> list[StructuralMatch]:
        return self.run_pattern(RAISE_HTTP_PATTERN)

    def find_models(self) -> list[StructuralMatch]:
        return self.run_pattern(PYDANTIC_MODEL_PATTERN)

    def _run(self, pattern: str, language: str) -> str:
        try:
            completed = process.run(
                [*shlex.split(self._ast_grep_cmd), "run", "-p", pattern, "-l", language, "--json"],
                timeout=process.MEDIUM_COMMAND_TIMEOUT_SECONDS,
                cwd=self._root,
                check=True,
                capture_output=True,
                text=True,
            )
        except process.CalledProcessError as error:
            raise AstGrepError(f"ast-grep failed: {error.stderr.strip()}") from error
        return completed.stdout
