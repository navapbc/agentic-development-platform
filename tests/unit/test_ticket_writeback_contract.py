from __future__ import annotations

import pytest
from services.integration.credentials import ConfigError
from services.integration.tickets.write_contract import (
    APPROVAL,
    COMMENT,
    STATUS,
    WRITE_SCOPES,
    WriteBackContext,
    WriteBackError,
    authorize_write_back,
    parse_write_back_scopes,
    write_back_audit,
)


def _ctx(
    issue_key: str = "ADSER-1", project: str = "ADSER", run_id: str = "run-1"
) -> WriteBackContext:
    return WriteBackContext(
        actor="alice", backend="rebar", project=project, issue_key=issue_key, run_id=run_id
    )


def test_authorized_write_back_passes() -> None:
    authorize_write_back(_ctx(), scope=STATUS, run_owner_issue_key="ADSER-1")


def test_write_back_to_ticket_not_owned_by_run_is_rejected() -> None:
    with pytest.raises(WriteBackError, match="not owned by run"):
        authorize_write_back(_ctx(issue_key="ADSER-9"), scope=STATUS, run_owner_issue_key="ADSER-1")


def test_write_back_with_wrong_project_is_rejected() -> None:
    with pytest.raises(WriteBackError, match="does not belong to project"):
        authorize_write_back(
            _ctx(issue_key="OTHER-1", project="ADSER"),
            scope=STATUS,
            run_owner_issue_key="OTHER-1",
        )


def test_scope_outside_allowlist_is_rejected() -> None:
    with pytest.raises(WriteBackError, match="outside the write-back allowlist"):
        authorize_write_back(
            _ctx(), scope=APPROVAL, run_owner_issue_key="ADSER-1", allowed_scopes=(STATUS, COMMENT)
        )


def test_unknown_scope_is_rejected() -> None:
    with pytest.raises(WriteBackError, match="unknown write-back scope"):
        authorize_write_back(_ctx(), scope="delete", run_owner_issue_key="ADSER-1")


def test_missing_target_ticket_is_rejected() -> None:
    with pytest.raises(WriteBackError, match="requires a target ticket"):
        authorize_write_back(_ctx(issue_key=""), scope=STATUS, run_owner_issue_key="")


def test_audit_record_names_actor_backend_project_ticket_run() -> None:
    record = write_back_audit(_ctx(), scope=APPROVAL, outcome="authorized")
    assert record["actor"] == "alice"
    assert record["backend"] == "rebar"
    assert record["project"] == "ADSER"
    assert record["ticket"] == "ADSER-1"
    assert record["run_id"] == "run-1"
    assert record["scope"] == APPROVAL


def test_parse_scopes_defaults_to_all_when_absent() -> None:
    assert parse_write_back_scopes({}) == WRITE_SCOPES


def test_parse_scopes_reads_allowlist() -> None:
    raw = {"write_back": {"allowed_scopes": ["status", "comment"]}}
    assert parse_write_back_scopes(raw) == (STATUS, COMMENT)


def test_parse_scopes_rejects_unknown_scope() -> None:
    with pytest.raises(ConfigError, match="unknown write-back scope"):
        parse_write_back_scopes({"write_back": {"allowed_scopes": ["bogus"]}})
