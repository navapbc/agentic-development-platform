"""Search-only repository-intelligence provider backed by a Zoekt shard index.

Answers repo_search_code with authoritative Source.ZOEKT provenance and does not
implement the structural query methods.
"""

from __future__ import annotations

import base64
import json
import shlex
import subprocess
from pathlib import Path

from services.repository_intelligence.indexing.zoekt_builder import (
    DEFAULT_ZOEKT_INDEX_CMD,
    DEFAULT_ZOEKT_SEARCH_CMD,
    build_zoekt_index,
    indexed_commit,
    resolve_commit,
)
from services.repository_intelligence.normalization.models import (
    IndexStatus,
    Location,
    Provenance,
    RepoRef,
    SearchHit,
    Source,
)

_ZOEKT_CONFIDENCE = 1.0


class ZoektSearchError(RuntimeError):
    """Raised when a Zoekt search invocation fails."""


def _decode_line(encoded: str) -> str:
    try:
        return base64.b64decode(encoded).decode("utf-8", errors="replace")
    except ValueError:
        return ""


def hits_from_jsonl(output: str, limit: int) -> list[SearchHit]:
    """Parse `zoekt -jsonl` output into score-ordered, deterministic search hits."""
    provenance = Provenance(source=Source.ZOEKT, confidence=_ZOEKT_CONFIDENCE)
    hits: list[SearchHit] = []
    for raw in output.splitlines():
        line = raw.strip()
        if not line:
            continue
        record = json.loads(line)
        path = record.get("FileName", "")
        for match in record.get("LineMatches") or []:
            fragments = match.get("LineFragments") or []
            start_col = int(fragments[0].get("LineOffset", 0)) if fragments else 0
            hits.append(
                SearchHit(
                    location=Location(
                        path=path,
                        start_line=int(match.get("LineNumber", 0)),
                        start_col=start_col,
                    ),
                    snippet=_decode_line(match.get("Line", "")).strip(),
                    score=float(match.get("Score", 0.0)),
                    provenance=provenance,
                )
            )
    hits.sort(key=lambda h: (-h.score, h.location.path, h.location.start_line))
    return hits[:limit]


class ZoektProvider:
    """Answers repo_search_code from a Zoekt shard index (search-only backend)."""

    def __init__(
        self,
        repo_root: str | Path,
        *,
        shard_dir: str | Path | None = None,
        commit: str | None = None,
        index_root: Path | None = None,
        zoekt_index_cmd: str = DEFAULT_ZOEKT_INDEX_CMD,
        zoekt_search_cmd: str = DEFAULT_ZOEKT_SEARCH_CMD,
    ) -> None:
        self._root = Path(repo_root).resolve()
        self._shard_dir = Path(shard_dir).resolve() if shard_dir is not None else None
        self._commit = commit
        self._index_root = index_root
        self._zoekt_index_cmd = zoekt_index_cmd
        self._zoekt_search_cmd = zoekt_search_cmd

    def _ensure_index(self) -> Path:
        if self._shard_dir is None:
            self._shard_dir, self._commit = build_zoekt_index(
                self._root,
                commit=self._commit,
                index_root=self._index_root,
                zoekt_index_cmd=self._zoekt_index_cmd,
            )
        return self._shard_dir

    def repo_search_code(self, repo: RepoRef, query: str, *, limit: int = 50) -> list[SearchHit]:
        shard_dir = self._ensure_index()
        output = self._run_search(shard_dir, query)
        return hits_from_jsonl(output, limit)

    def _run_search(self, shard_dir: Path, query: str) -> str:
        try:
            completed = subprocess.run(
                [
                    *shlex.split(self._zoekt_search_cmd),
                    "-index_dir",
                    str(shard_dir),
                    "-jsonl",
                    query,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as error:
            raise ZoektSearchError(f"zoekt search failed: {error.stderr.strip()}") from error
        return completed.stdout

    def repo_get_index_status(self, repo: RepoRef) -> IndexStatus:
        shard_dir = self._ensure_index()
        recorded = indexed_commit(shard_dir)
        head = self._current_commit()
        return IndexStatus(
            repo=repo,
            indexed_commit=recorded,
            is_fresh=recorded is not None and head is not None and recorded == head,
            languages=(),
        )

    def _current_commit(self) -> str | None:
        try:
            return resolve_commit(self._root)
        except (subprocess.CalledProcessError, FileNotFoundError):
            return None
