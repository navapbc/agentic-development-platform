from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from services.integration.credentials import ConfigError
from services.integration.tickets.store_config import (
    TicketsStoreConfig,
    load_tickets_store_config,
    parse_tickets_store_config,
)


def _base() -> dict[str, Any]:
    return {
        "config_version": 1,
        "store": {"bucket": "rebar-tickets", "prefix": "tickets/"},
        "roles": {
            "read_only": "role/rebar-tickets-reader",
            "write": "role/rebar-tickets-writer",
        },
        "provenance": {"require_actor": True, "require_run_id": True},
        "isolation": {
            "evidence_bucket": "agentic-evidence",
            "audit_bucket": "agentic-audit",
            "evidence_writer_role": "role/evidence-writer",
            "audit_writer_role": "role/audit-writer",
        },
    }


def test_valid_config_parses_roles() -> None:
    config = parse_tickets_store_config(_base())
    assert isinstance(config, TicketsStoreConfig)
    assert config.roles.read_only == "role/rebar-tickets-reader"
    assert config.roles.write == "role/rebar-tickets-writer"
    assert config.store.bucket == "rebar-tickets"


def test_read_role_equal_to_write_role_fails() -> None:
    raw = _base()
    raw["roles"]["write"] = raw["roles"]["read_only"]
    with pytest.raises(ConfigError, match="read-only polling role must differ"):
        parse_tickets_store_config(raw)


def test_role_equal_to_evidence_writer_fails() -> None:
    raw = _base()
    raw["roles"]["write"] = raw["isolation"]["evidence_writer_role"]
    with pytest.raises(ConfigError, match="evidence or audit writer role"):
        parse_tickets_store_config(raw)


def test_role_equal_to_audit_writer_fails() -> None:
    raw = _base()
    raw["roles"]["read_only"] = raw["isolation"]["audit_writer_role"]
    with pytest.raises(ConfigError, match="evidence or audit writer role"):
        parse_tickets_store_config(raw)


def test_store_reusing_evidence_bucket_fails() -> None:
    raw = _base()
    raw["store"]["bucket"] = raw["isolation"]["evidence_bucket"]
    with pytest.raises(ConfigError, match="must not reuse the evidence or audit bucket"):
        parse_tickets_store_config(raw)


def test_store_reusing_audit_bucket_fails() -> None:
    raw = _base()
    raw["store"]["bucket"] = raw["isolation"]["audit_bucket"]
    with pytest.raises(ConfigError, match="must not reuse the evidence or audit bucket"):
        parse_tickets_store_config(raw)


def test_missing_provenance_fails() -> None:
    raw = _base()
    raw["provenance"]["require_run_id"] = False
    with pytest.raises(ConfigError, match="require actor and run provenance"):
        parse_tickets_store_config(raw)


def test_missing_write_role_fails() -> None:
    raw = _base()
    raw["roles"]["write"] = ""
    with pytest.raises(ConfigError, match="roles.write is required"):
        parse_tickets_store_config(raw)


def test_committed_example_parses() -> None:
    example = (
        Path(__file__).resolve().parents[2] / "config" / "local" / "tickets.yaml.example"
    )
    raw = yaml.safe_load(example.read_text(encoding="utf-8"))
    config = parse_tickets_store_config(raw)
    assert config.roles.read_only != config.roles.write


def test_loader_reads_operator_file(tmp_path: Path) -> None:
    directory = tmp_path / "config" / "local"
    directory.mkdir(parents=True)
    (directory / "tickets.yaml").write_text(yaml.safe_dump(_base()), encoding="utf-8")

    config = load_tickets_store_config(environment="local", root=tmp_path)

    assert config.store.bucket == "rebar-tickets"
