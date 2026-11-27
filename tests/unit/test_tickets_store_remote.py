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


def _parse(raw: dict[str, Any]) -> Any:
    """Parse the tenant sections of raw against its isolation section, the platform's file."""
    tenant = {key: value for key, value in raw.items() if key != "isolation"}
    return parse_tickets_store_config(tenant, raw["isolation"])


@pytest.fixture
def store(tmp_path: Path) -> str:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / store_remote.TICKETS_WORKTREE).mkdir()
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
    config = _parse(_raw())

    assert config.store.remote_url("rebar-tickets-reader") == READER_URL


def test_equal_profiles_are_rejected():
    with pytest.raises(ConfigError, match="reader profile must differ"):
        _parse(_raw(writer_profile="rebar-tickets-reader"))


def test_a_store_without_a_remote_reads_as_local_only(store):
    assert read_store_remote(store) is None


def test_configure_sets_the_reader_fetch_url_and_the_writer_push_url(store):
    configure_store_remote(store, _parse(_raw()))

    assert read_store_remote(store) == StoreRemote("origin", READER_URL, WRITER_URL)


def test_configure_replaces_an_existing_remote(store):
    subprocess.run(["git", "-C", store, "remote", "add", "origin", "s3://other/x"], check=True)

    configure_store_remote(store, _parse(_raw()))

    assert read_store_remote(store) == StoreRemote("origin", READER_URL, WRITER_URL)


def test_configure_requires_both_profiles(store):
    with pytest.raises(ConfigError, match="reader_profile and store.writer_profile"):
        configure_store_remote(store, _parse(_raw(writer_profile="")))


def test_check_accepts_the_configured_remote(profiles):
    profiles()

    check_store_remote(StoreRemote("origin", READER_URL, WRITER_URL), _parse(_raw()))


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
        check_store_remote(StoreRemote("origin", fetch, push), _parse(_raw()))


def test_check_rejects_a_profile_that_assumes_another_role(profiles):
    profiles(writer_role="arn:aws:iam::111111111111:role/evidence-writer")

    with pytest.raises(ConfigError, match="assumes"):
        check_store_remote(StoreRemote("origin", READER_URL, WRITER_URL), _parse(_raw()))


def test_check_rejects_an_undefined_profile(monkeypatch, tmp_path):
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "missing"))

    with pytest.raises(ConfigError, match="is not defined"):
        check_store_remote(StoreRemote("origin", READER_URL, WRITER_URL), _parse(_raw()))


def _preflight_env(monkeypatch, store: str, raw: dict[str, Any]) -> Settings:
    from services.integration.tickets import rebar_config, store_config

    monkeypatch.setattr(
        rebar_config,
        "load_rebar_backend_config",
        lambda: rebar_config.RebarBackendConfig(repo_root=store),
    )
    monkeypatch.setattr(store_config, "load_tickets_store_config", lambda: _parse(raw))
    return Settings(ticket_backend="rebar")


def test_preflight_skips_a_jira_deployment(monkeypatch):
    monkeypatch.setattr(store_remote, "read_store_remote", pytest.fail)

    tickets_store_preflight(Settings(ticket_backend="jira"))


def test_preflight_skips_a_local_only_store(monkeypatch, store):
    tickets_store_preflight(_preflight_env(monkeypatch, store, {}))


def test_preflight_validates_isolation_for_an_s3_store(monkeypatch, store, profiles):
    profiles()
    configure_store_remote(store, _parse(_raw()))
    raw = _raw()
    raw["isolation"]["evidence_bucket"] = "rebar-tickets"
    settings = _preflight_env(monkeypatch, store, raw)

    with pytest.raises(ConfigError, match="evidence or audit bucket"):
        tickets_store_preflight(settings)


def test_preflight_accepts_a_consistent_s3_store(monkeypatch, store, profiles):
    profiles()
    configure_store_remote(store, _parse(_raw()))

    tickets_store_preflight(_preflight_env(monkeypatch, store, _raw()))


def test_seed_creates_the_store_with_the_split_remote_and_runs_rebar_init(monkeypatch, tmp_path):
    rebar_calls: list[tuple[str, ...]] = []
    run = store_remote.process.run

    def fake_run(command, **kwargs):
        if command[0] == "rebar":
            assert kwargs["cwd"] == root
            rebar_calls.append(tuple(command[1:]))
            return subprocess.CompletedProcess(command, 0, "", "")
        if "ls-remote" in command:
            return subprocess.CompletedProcess(command, 0, "", "")
        return run(command, **kwargs)

    monkeypatch.setattr(store_remote.process, "run", fake_run)
    root = str(tmp_path / "store")

    seeded = store_remote.seed_store(root, _parse(_raw()), "2940-c3f4")

    assert seeded is True
    assert read_store_remote(root) == StoreRemote("origin", READER_URL, WRITER_URL)
    assert rebar_calls == [("init", "--silent"), ("identity", "use", "2940-c3f4")]


def _seed_against(monkeypatch, probe_code: int, push_code: int = 0) -> list[list[str]]:
    pushes: list[list[str]] = []
    run = store_remote.process.run

    def fake_run(command, **kwargs):
        if command[0] == "rebar":
            return subprocess.CompletedProcess(command, 0, "", "")
        if "ls-remote" in command:
            return subprocess.CompletedProcess(command, probe_code, "", "")
        if "push" in command:
            pushes.append(command)
            return subprocess.CompletedProcess(command, push_code, "", "denied")
        return run(command, **kwargs)

    monkeypatch.setattr(store_remote.process, "run", fake_run)
    return pushes


def test_a_seed_against_an_empty_bucket_pushes_the_new_store(monkeypatch, tmp_path):
    pushes = _seed_against(monkeypatch, probe_code=2)
    root = str(tmp_path / "store")

    store_remote.seed_store(root, _parse(_raw()))

    assert [command[-3:] for command in pushes] == [["--quiet", "origin", "tickets"]]


def test_a_seed_that_attaches_to_a_bucket_pushes_nothing(monkeypatch, tmp_path):
    pushes = _seed_against(monkeypatch, probe_code=0)

    store_remote.seed_store(str(tmp_path / "store"), _parse(_raw()))

    assert pushes == []


def test_a_failed_push_of_a_new_store_refuses_the_seed(monkeypatch, tmp_path):
    _seed_against(monkeypatch, probe_code=2, push_code=1)

    with pytest.raises(ConfigError, match="empty bucket of origin failed: denied"):
        store_remote.seed_store(str(tmp_path / "store"), _parse(_raw()))


def test_seed_leaves_an_initialized_store_alone(monkeypatch, store):
    monkeypatch.setattr(store_remote, "configure_store_remote", pytest.fail)

    assert store_remote.seed_store(store, _parse(_raw())) is False


def test_seed_completes_a_store_whose_rebar_init_did_not_finish(monkeypatch, tmp_path):
    partial = str(tmp_path / "store")
    subprocess.run(["git", "init", "-q", partial], check=True)
    rebar_calls: list[tuple[str, ...]] = []
    run = store_remote.process.run

    def fake_run(command, **kwargs):
        if command[0] == "rebar":
            rebar_calls.append(tuple(command[1:]))
            return subprocess.CompletedProcess(command, 0, "", "")
        if "ls-remote" in command:
            return subprocess.CompletedProcess(command, 0, "", "")
        return run(command, **kwargs)

    monkeypatch.setattr(store_remote.process, "run", fake_run)

    assert store_remote.seed_store(partial, _parse(_raw())) is True
    assert read_store_remote(partial) == StoreRemote("origin", READER_URL, WRITER_URL)
    assert rebar_calls == [("init", "--silent")]


@pytest.mark.parametrize(("current", "expected"), [(None, 1), ("other-id", 1), ("2940-c3f4", 0)])
def test_the_configured_identity_is_selected_unless_it_is_current(
    monkeypatch, store, current, expected
):
    import rebar

    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(rebar, "resolve_current_identity", lambda repo_root: current)
    monkeypatch.setattr(store_remote, "_rebar", lambda repo_root, *args: calls.append(args))

    store_remote.select_store_identity(store, "2940-c3f4")

    assert calls == [("identity", "use", "2940-c3f4")] * expected


def test_a_failed_seed_names_the_remote_probe_error(monkeypatch, tmp_path):
    run = store_remote.process.run

    def fake_run(command, **kwargs):
        if command[0] == "rebar":
            return subprocess.CompletedProcess(
                command, 1, "", "could not determine whether origin/tickets exists"
            )
        if "ls-remote" in command:
            return subprocess.CompletedProcess(
                command, 128, "", "fatal: Unable to locate credentials"
            )
        return run(command, **kwargs)

    monkeypatch.setattr(store_remote.process, "run", fake_run)

    with pytest.raises(ConfigError, match="exited 128: fatal: Unable to locate credentials"):
        store_remote.seed_store(str(tmp_path / "store"), _parse(_raw()))


def test_preflight_seeds_an_absent_s3_store_before_checking_it(monkeypatch, tmp_path, profiles):
    profiles()
    root = str(tmp_path / "store")
    seeded: list[str] = []

    def seed(repo_root, config, identity_id=""):
        seeded.append(repo_root)
        subprocess.run(["git", "init", "-q", repo_root], check=True)
        configure_store_remote(repo_root, config)
        return True

    monkeypatch.setattr(store_remote, "seed_store", seed)

    tickets_store_preflight(_preflight_env(monkeypatch, root, _raw()))

    assert seeded == [root]


def test_preflight_does_not_seed_without_a_store_config(monkeypatch, tmp_path):
    from services.integration.tickets import store_config

    settings = _preflight_env(monkeypatch, str(tmp_path / "store"), _raw())

    def missing():
        raise ConfigError("tickets.yaml has no store section")

    monkeypatch.setattr(store_config, "load_tickets_store_config", missing)
    monkeypatch.setattr(store_remote, "seed_store", pytest.fail)

    with pytest.raises(ConfigError):
        tickets_store_preflight(settings)
