"""Platform-owned store for each run's predicted file touch set.

The touch set is run-state, not ticket data: it is computed from the
repository-intelligence context at a run's base and read by peer runs to detect
overlap. Holding it in a platform table rather than a ticket backend lets
detection run on any backend, including Jira, which has no per-ticket
file-impact store.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


class FileImpactStore:
    """Read and write a run's predicted touch set keyed by ticket id."""

    def __init__(self) -> None:
        self._engine: Any = None

    def _get_engine(self) -> Any:
        if self._engine is None:
            from scripts.config import get_settings
            from sqlalchemy.ext.asyncio import create_async_engine

            self._engine = create_async_engine(
                get_settings().database_url, future=True, pool_pre_ping=True
            )
        return self._engine

    async def write(self, task_id: str, paths: Sequence[str], execution_id: str) -> None:
        from database.models.schema import run_file_impact
        from sqlalchemy.dialects.postgresql import insert

        values = list(dict.fromkeys(str(path) for path in paths if path))
        stmt = (
            insert(run_file_impact)
            .values(task_id=task_id, execution_id=execution_id, paths=values)
            .on_conflict_do_update(
                index_elements=[run_file_impact.c.task_id],
                set_={"execution_id": execution_id, "paths": values},
            )
        )
        async with self._get_engine().begin() as conn:
            await conn.execute(stmt)

    async def read(self, task_id: str) -> tuple[str, ...]:
        from database.models.schema import run_file_impact
        from sqlalchemy import select

        stmt = select(run_file_impact.c.paths).where(run_file_impact.c.task_id == task_id)
        async with self._get_engine().connect() as conn:
            row = (await conn.execute(stmt)).first()
        if row is None or not row.paths:
            return ()
        return tuple(str(path) for path in row.paths)
