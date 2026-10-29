"""Tests for the rebar ticket store's split S3 remote and its startup check."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from scripts.config import Settings
from services.integration.credentials import ConfigError
from services.integration.tickets import store_remote
from services.integration.tickets.store_config import parse_tickets_store_config
from services.integration.tickets.store_remote import (
    StoreRemote,
    check_store_remote,
    configure_store_remote,
    read_store_remote,
    tickets_store_preflight,
)

READER_URL = "s3://rebar-tickets-reader@rebar-tickets/tickets"
WRITER_URL = "s3://rebar-tickets-writer@rebar-tickets/tickets"
READER_ROLE = "arn:aws:iam::111111111111:role/rebar-tickets-reader"
WRITER_ROLE = "arn:aws:iam::111111111111:role/rebar-tickets-writer"


def _raw(**store: Any) -> dict[str, Any]:
    return {
        "store": {
            "bucket": "rebar-tickets",
            "prefix": "tickets/",
            "reader_profile": "rebar-tickets-reader",
            "writer_profile": "rebar-tickets-writer",
            **store,
        },
        "roles": {"read_only": READER_ROLE, "write": WRITER_ROLE},
        "provenance": {"require_actor": True, "require_run_id": True},
        "isolation": {
            "evidence_bucket": "agentic-evidence",
            "audit_bucket": "agentic-audit",
            "evidence_writer_role": "arn:aws:iam::111111111111:role/evidence-writer",
            "audit_writer_role": "arn:aws:iam::111111111111:role/audit-writer",
        },
    }


@pytest.fixture
def store(tmp_path: Path) -> str:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return str(tmp_path)


@pytest.fixture
def profiles(monkeypatch, tmp_path: Path):
    def write(reader_role: str = READER_ROLE, writer_role: str = WRITER_ROLE) -> None:
        config = tmp_path / "aws-config"
        config.write_text(
            f"[profile rebar-tickets-reader]\nrole_arn = {reader_role}\n"
            f"credential_source = Environment\n\n"
            f"[profile rebar-tickets-writer]\nrole_arn = {writer_role}\n"
            f"credential_source = Environment\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config))

    return write


def test_remote_urls_name_the_profile_bucket_and_normalized_prefix():
    config = parse_tickets_store_config(_raw())

    assert config.store.remote_url("rebar-tickets-reader") == READER_URL


def test_equal_profiles_are_rejected():
    with pytest.raises(ConfigError, match="reader profile must differ"):
        parse_tickets_store_config(_raw(writer_profile="rebar-tickets-reader"))


def test_a_store_without_a_remote_reads_as_local_only(store):
    assert read_store_remote(store) is None


def test_configure_sets_the_reader_fetch_url_and_the_writer_push_url(store):
    configure_store_remote(store, parse_tickets_store_config(_raw()))

    assert read_store_remote(store) == StoreRemote("origin", READER_URL, WRITER_URL)


def test_configure_replaces_an_existing_remote(store):
    subprocess.run(["git", "-C", store, "remote", "add", "origin", "s3://other/x"], check=True)

    configure_store_remote(store, parse_tickets_store_config(_raw()))

    assert read_store_remote(store) == StoreRemote("origin", READER_URL, WRITER_URL)


def test_configure_requires_both_profiles(store):
    with pytest.raises(ConfigError, match="reader_profile and store.writer_profile"):
        configure_store_remote(store, parse_tickets_store_config(_raw(writer_profile="")))


def test_check_accepts_the_configured_remote(profiles):
    profiles()

    check_store_remote(
        StoreRemote("origin", READER_URL, WRITER_URL), parse_tickets_store_config(_raw())
    )


@pytest.mark.parametrize(
    ("fetch", "push", "message"),
    [
        ("s3://rebar-tickets-reader@other-bucket/tickets", WRITER_URL, "fetches from"),
        (READER_URL, READER_URL, "pushes to"),
        (WRITER_URL, WRITER_URL, "fetches from"),
    ],
)
def test_check_rejects_a_remote_that_does_not_match_the_config(profiles, fetch, push, message):
    profiles()

    with pytest.raises(ConfigError, match=message):
        check_store_remote(StoreRemote("origin", fetch, push), parse_tickets_store_config(_raw()))


def test_check_rejects_a_profile_that_assumes_another_role(profiles):
    profiles(writer_role="arn:aws:iam::111111111111:role/evidence-writer")

    with pytest.raises(ConfigError, match="assumes"):
        check_store_remote(
            StoreRemote("origin", READER_URL, WRITER_URL), parse_tickets_store_config(_raw())
        )


def test_check_rejects_an_undefined_profile(monkeypatch, tmp_path):
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "missing"))

    with pytest.raises(ConfigError, match="is not defined"):
        check_store_remote(
            StoreRemote("origin", READER_URL, WRITER_URL), parse_tickets_store_config(_raw())
        )


def _preflight_env(monkeypatch, store: str, raw: dict[str, Any]) -> Settings:
    from services.integration.tickets import rebar_config, store_config

    monkeypatch.setattr(
        rebar_config,
        "load_rebar_backend_config",
        lambda _settings: rebar_config.RebarBackendConfig(repo_root=store),
    )
    monkeypatch.setattr(
        store_config, "load_tickets_store_config", lambda _settings: parse_tickets_store_config(raw)
    )
    return Settings(ticket_backend="rebar")


def test_preflight_skips_a_jira_deployment(monkeypatch):
    monkeypatch.setattr(store_remote, "read_store_remote", pytest.fail)

    tickets_store_preflight(Settings(ticket_backend="jira"))


def test_preflight_skips_a_local_only_store(monkeypatch, store):
    tickets_store_preflight(_preflight_env(monkeypatch, store, {}))


def test_preflight_validates_isolation_for_an_s3_store(monkeypatch, store, profiles):
    profiles()
    configure_store_remote(store, parse_tickets_store_config(_raw()))
    raw = _raw()
    raw["isolation"]["evidence_bucket"] = "rebar-tickets"
    settings = _preflight_env(monkeypatch, store, raw)

    with pytest.raises(ConfigError, match="evidence or audit bucket"):
        tickets_store_preflight(settings)


def test_preflight_accepts_a_consistent_s3_store(monkeypatch, store, profiles):
    profiles()
    configure_store_remote(store, parse_tickets_store_config(_raw()))

    tickets_store_preflight(_preflight_env(monkeypatch, store, _raw()))
