"""Data checks of the claim tenant migration, run against SQLite through a stand-in for op.

The stand-in executes the migration's SQL and applies column changes, and skips constraint
changes, which SQLite cannot alter in place. The tables therefore carry no constraints, and
the assertions cover the rows each step leaves.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
from services.coordination.claims import claim_id_for

_MIGRATION = Path(__file__).parents[2] / "database/migrations/versions/0014_claim_tenant.py"


_ADD_TENANT = {
    "work_claims": "ALTER TABLE agent_platform.work_claims ADD COLUMN tenant TEXT",
    "sequence_gate": "ALTER TABLE agent_platform.sequence_gate ADD COLUMN tenant TEXT",
}
_DROP_TENANT = {
    "work_claims": "ALTER TABLE agent_platform.work_claims DROP COLUMN tenant",
    "sequence_gate": "ALTER TABLE agent_platform.sequence_gate DROP COLUMN tenant",
}


class _SqliteOp:
    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db

    def f(self, name: str) -> str:
        return name

    def execute(self, statement: str) -> None:
        self._db.execute(statement)

    def add_column(self, table: str, column, schema: str) -> None:
        self._db.execute(_ADD_TENANT[table])

    def drop_column(self, table: str, column: str, schema: str) -> None:
        self._db.execute(_DROP_TENANT[table])

    def alter_column(self, *args, **kwargs) -> None:
        return None

    def drop_constraint(self, *args, **kwargs) -> None:
        return None

    def create_unique_constraint(self, *args, **kwargs) -> None:
        return None

    def create_primary_key(self, *args, **kwargs) -> None:
        return None


def _postgres_functions(db: sqlite3.Connection) -> None:
    db.create_function("chr", 1, chr)
    db.create_function("convert_to", 2, lambda value, _encoding: value.encode())
    db.create_function("sha256", 1, lambda value: hashlib.sha256(value).digest())
    db.create_function("encode", 2, lambda value, _format: value.hex())


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:")
    connection.execute("ATTACH DATABASE ':memory:' AS agent_platform")
    _postgres_functions(connection)
    connection.executescript(
        """
        CREATE TABLE agent_platform.executions (execution_id TEXT, task_id TEXT, tenant TEXT);
        CREATE TABLE agent_platform.work_claims (
            claim_id TEXT, task_id TEXT, work_unit TEXT, execution_id TEXT, state TEXT,
            claim_version INTEGER, created_at TEXT
        );
        CREATE TABLE agent_platform.sequence_gate (
            task_id TEXT, current_sequence INTEGER, updated_at TEXT
        );
        """
    )
    yield connection
    connection.close()


@pytest.fixture
def migration(db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location("claim_tenant_migration", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "op", _SqliteOp(db))
    return module


def _seed(db: sqlite3.Connection, executions, claims, gates) -> None:
    db.executemany("INSERT INTO agent_platform.executions VALUES (?, ?, ?)", executions)
    db.executemany(
        "INSERT INTO agent_platform.work_claims VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (claim_id_for(task, unit, ""), task, unit, execution, state, version, created)
            for task, unit, execution, state, version, created in claims
        ],
    )
    db.executemany(
        "INSERT INTO agent_platform.sequence_gate VALUES (?, ?, '2026-11-01')",
        gates,
    )


def _gates(db: sqlite3.Connection) -> set[tuple]:
    return set(
        db.execute("SELECT tenant, task_id, current_sequence FROM agent_platform.sequence_gate")
    )


def test_the_gate_moves_only_to_the_tenant_that_keeps_the_claim(db, migration):
    _seed(
        db,
        executions=[("e-alpha", "ADEV-1", "alpha"), ("e-beta", "ADEV-1", "beta")],
        claims=[("ADEV-1", "unit-1", "e-alpha", "completed", 3, "2026-11-01")],
        gates=[("ADEV-1", 300)],
    )

    migration.upgrade()

    assert _gates(db) == {("alpha", "ADEV-1", 300)}
    assert list(db.execute("SELECT tenant, claim_id FROM agent_platform.work_claims")) == [
        ("alpha", claim_id_for("ADEV-1", "unit-1", "alpha"))
    ]


def test_the_gate_of_a_claim_whose_execution_is_gone_moves_to_the_empty_tenant(db, migration):
    _seed(
        db,
        executions=[("e-alpha", "ADEV-2", "alpha")],
        claims=[("ADEV-2", "unit-1", "e-gone", "released", 2, "2026-11-01")],
        gates=[("ADEV-2", 200), ("ADEV-3", 100)],
    )

    migration.upgrade()

    assert _gates(db) == {("", "ADEV-2", 200), ("", "ADEV-3", 100)}


def test_the_downgrade_keeps_the_highest_gate_and_the_live_claim_per_task(db, migration):
    _seed(
        db,
        executions=[("e-alpha", "ADEV-1", "alpha"), ("e-beta", "ADEV-1", "beta")],
        claims=[("ADEV-1", "unit-1", "e-alpha", "completed", 3, "2026-11-01")],
        gates=[("ADEV-1", 300)],
    )
    migration.upgrade()
    db.execute(
        "INSERT INTO agent_platform.work_claims VALUES (?, 'ADEV-1', 'unit-1', 'e-beta', "
        "'claimed', 1, '2026-11-02', 'beta')",
        (claim_id_for("ADEV-1", "unit-1", "beta"),),
    )
    db.execute(
        "INSERT INTO agent_platform.sequence_gate VALUES ('ADEV-1', 100, '2026-11-02', 'beta')"
    )

    migration.downgrade()

    assert set(db.execute("SELECT * FROM agent_platform.sequence_gate")) == {
        ("ADEV-1", 300, "2026-11-01")
    }
    assert list(db.execute("SELECT claim_id, execution_id FROM agent_platform.work_claims")) == [
        (claim_id_for("ADEV-1", "unit-1", ""), "e-beta")
    ]
