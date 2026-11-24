from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from services.integration.credentials import ConfigError
from services.integration.tickets.rebar_config import parse_rebar_backend_config
from services.integration.tickets.store_config import (
    TicketsStoreConfig,
    load_tickets_store_config,
    parse_tickets_store_config,
)

_CONFIG = Path(__file__).resolve().parents[2] / "config" / "local"
_EXAMPLE = _CONFIG / "tenants" / "tenant.example" / "tickets.yaml.example"
_ISOLATION_EXAMPLE = _CONFIG / "tickets_isolation.yaml.example"


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


def _parse(raw: dict[str, Any]) -> TicketsStoreConfig:
    """Parse the tenant sections of raw against its isolation section, the platform's file."""
    tenant = {key: value for key, value in raw.items() if key != "isolation"}
    return parse_tickets_store_config(tenant, raw["isolation"])


def test_valid_config_parses_roles() -> None:
    config = _parse(_base())
    assert isinstance(config, TicketsStoreConfig)
    assert config.roles.read_only == "role/rebar-tickets-reader"
    assert config.roles.write == "role/rebar-tickets-writer"
    assert config.store.bucket == "rebar-tickets"


def test_read_role_equal_to_write_role_fails() -> None:
    raw = _base()
    raw["roles"]["write"] = raw["roles"]["read_only"]
    with pytest.raises(ConfigError, match="read-only polling role must differ"):
        _parse(raw)


def test_role_equal_to_evidence_writer_fails() -> None:
    raw = _base()
    raw["roles"]["write"] = raw["isolation"]["evidence_writer_role"]
    with pytest.raises(ConfigError, match="evidence or audit writer role"):
        _parse(raw)


def test_role_equal_to_audit_writer_fails() -> None:
    raw = _base()
    raw["roles"]["read_only"] = raw["isolation"]["audit_writer_role"]
    with pytest.raises(ConfigError, match="evidence or audit writer role"):
        _parse(raw)


def test_store_reusing_evidence_bucket_fails() -> None:
    raw = _base()
    raw["store"]["bucket"] = raw["isolation"]["evidence_bucket"]
    with pytest.raises(ConfigError, match="must not reuse the evidence or audit bucket"):
        _parse(raw)


def test_store_reusing_audit_bucket_fails() -> None:
    raw = _base()
    raw["store"]["bucket"] = raw["isolation"]["audit_bucket"]
    with pytest.raises(ConfigError, match="must not reuse the evidence or audit bucket"):
        _parse(raw)


def test_missing_provenance_fails() -> None:
    raw = _base()
    raw["provenance"]["require_run_id"] = False
    with pytest.raises(ConfigError, match="require actor and run provenance"):
        _parse(raw)


def test_missing_write_role_fails() -> None:
    raw = _base()
    raw["roles"]["write"] = ""
    with pytest.raises(ConfigError, match="roles.write is required"):
        _parse(raw)


def test_committed_example_parses() -> None:
    raw = yaml.safe_load(_EXAMPLE.read_text(encoding="utf-8"))
    isolation = yaml.safe_load(_ISOLATION_EXAMPLE.read_text(encoding="utf-8"))
    config = parse_tickets_store_config(raw, isolation)
    assert config.roles.read_only != config.roles.write


def test_a_tickets_file_that_sets_isolation_is_refused() -> None:
    raw = _base()
    with pytest.raises(ConfigError, match="tickets.yaml sets isolation"):
        parse_tickets_store_config(raw, raw["isolation"])


def test_committed_example_names_an_absolute_store_root() -> None:
    raw = yaml.safe_load(_EXAMPLE.read_text(encoding="utf-8"))

    assert Path(parse_rebar_backend_config(raw).repo_root).is_absolute()


def test_a_relative_store_root_is_refused_because_host_and_worker_disagree() -> None:
    raw = {"backend": {"repo_root": ".agent-workspaces/rebar-tickets"}}

    with pytest.raises(ConfigError, match="must be an absolute path"):
        parse_rebar_backend_config(raw)


def test_loader_reads_operator_file(tmp_path: Path) -> None:
    directory = tmp_path / "config" / "local"
    directory.mkdir(parents=True)
    raw = _base()
    isolation = {"config_version": 1, **raw.pop("isolation")}
    (directory / "tickets.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    (directory / "tickets_isolation.yaml").write_text(yaml.safe_dump(isolation), encoding="utf-8")

    config = load_tickets_store_config(root=tmp_path)

    assert config.store.bucket == "rebar-tickets"


def test_the_loader_reads_the_tenants_tickets_file_and_the_platforms_isolation(
    monkeypatch, tmp_path: Path
) -> None:
    from tests.support import configure_tenant

    bundle = configure_tenant(monkeypatch, tmp_path)
    raw = _base()
    isolation = {"config_version": 1, **raw.pop("isolation")}
    (bundle / "tickets.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    (tmp_path / "tickets_isolation.yaml").write_text(yaml.safe_dump(isolation), encoding="utf-8")
    stray = {**isolation, "evidence_bucket": "rebar-tickets"}
    (bundle / "tickets_isolation.yaml").write_text(yaml.safe_dump(stray), encoding="utf-8")

    config = load_tickets_store_config()

    assert config.store.bucket == "rebar-tickets"


def test_the_loader_refuses_a_platform_without_an_isolation_file(
    monkeypatch, tmp_path: Path
) -> None:
    from tests.support import configure_tenant

    bundle = configure_tenant(monkeypatch, tmp_path)
    raw = _base()
    raw.pop("isolation")
    (bundle / "tickets.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(ConfigError, match="tickets_isolation.yaml"):
        load_tickets_store_config()
