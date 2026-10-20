"""Parsing and isolation validation for the rebar ticket-store credentials.

The rebar ticket store uses its own bucket or prefix with distinct roles for
read-only polling and for writes. This module fails a config closed when those
roles collapse together, when they reuse the evidence or audit writer roles, or
when the store reuses an evidence or audit bucket. It also requires actor and
run provenance on tickets-branch ref writes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from services.integration.credentials import ConfigError


@dataclass(frozen=True)
class TicketsStore:
    bucket: str
    prefix: str


@dataclass(frozen=True)
class TicketsRoles:
    read_only: str
    write: str


@dataclass(frozen=True)
class TicketsProvenance:
    require_actor: bool
    require_run_id: bool


@dataclass(frozen=True)
class TicketsIsolation:
    evidence_bucket: str
    audit_bucket: str
    evidence_writer_role: str
    audit_writer_role: str


@dataclass(frozen=True)
class TicketsStoreConfig:
    store: TicketsStore
    roles: TicketsRoles
    provenance: TicketsProvenance
    isolation: TicketsIsolation


def _section(raw: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = raw.get(key)
    if not isinstance(value, Mapping):
        raise ConfigError(f"tickets store config section {key!r} is missing or not a mapping")
    return value


def _required_str(section: Mapping[str, Any], key: str, source: str) -> str:
    value = section.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"tickets store config {source}.{key} is required")
    return value.strip()


def _required_bool(section: Mapping[str, Any], key: str, source: str) -> bool:
    value = section.get(key)
    if not isinstance(value, bool):
        raise ConfigError(f"tickets store config {source}.{key} must be a boolean")
    return value


def parse_tickets_store_config(raw: Mapping[str, Any]) -> TicketsStoreConfig:
    store_raw = _section(raw, "store")
    roles_raw = _section(raw, "roles")
    provenance_raw = _section(raw, "provenance")
    isolation_raw = _section(raw, "isolation")

    store = TicketsStore(
        bucket=_required_str(store_raw, "bucket", "store"),
        prefix=str(store_raw.get("prefix", "")).strip(),
    )
    roles = TicketsRoles(
        read_only=_required_str(roles_raw, "read_only", "roles"),
        write=_required_str(roles_raw, "write", "roles"),
    )
    provenance = TicketsProvenance(
        require_actor=_required_bool(provenance_raw, "require_actor", "provenance"),
        require_run_id=_required_bool(provenance_raw, "require_run_id", "provenance"),
    )
    isolation = TicketsIsolation(
        evidence_bucket=_required_str(isolation_raw, "evidence_bucket", "isolation"),
        audit_bucket=_required_str(isolation_raw, "audit_bucket", "isolation"),
        evidence_writer_role=_required_str(isolation_raw, "evidence_writer_role", "isolation"),
        audit_writer_role=_required_str(isolation_raw, "audit_writer_role", "isolation"),
    )

    _validate(store, roles, provenance, isolation)
    return TicketsStoreConfig(store=store, roles=roles, provenance=provenance, isolation=isolation)


def _validate(
    store: TicketsStore,
    roles: TicketsRoles,
    provenance: TicketsProvenance,
    isolation: TicketsIsolation,
) -> None:
    if roles.read_only == roles.write:
        raise ConfigError("tickets store read-only polling role must differ from the write role")
    forbidden_roles = {isolation.evidence_writer_role, isolation.audit_writer_role}
    if roles.read_only in forbidden_roles or roles.write in forbidden_roles:
        raise ConfigError("tickets store roles must not equal the evidence or audit writer role")
    if store.bucket in {isolation.evidence_bucket, isolation.audit_bucket}:
        raise ConfigError("tickets store must not reuse the evidence or audit bucket")
    if not (provenance.require_actor and provenance.require_run_id):
        raise ConfigError("tickets store config must require actor and run provenance on writes")


def load_tickets_store_config(
    settings: Any = None, *, environment: str | None = None, root: Path | None = None
) -> TicketsStoreConfig:
    from scripts.config import get_settings

    from services.integration.credentials import load_env_config, require_current_config

    resolved = settings or get_settings()
    env = environment or resolved.environment
    require_current_config("tickets", environment=env, root=root)
    return parse_tickets_store_config(load_env_config("tickets", environment=env, root=root))
