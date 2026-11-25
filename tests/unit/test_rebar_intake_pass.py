from __future__ import annotations

from types import SimpleNamespace

import pytest
from services.orchestration.temporal_activities.intake import (
    IntakeReconcileRequest,
    reconcile_intake_pass,
)
from tests.support import hermetic_settings


async def test_a_rebar_tenant_reconciles_without_jira_config(monkeypatch, tmp_path):
    contexts = []

    def _jira_config_missing():
        raise AssertionError("the rebar pass read jira.yaml")

    async def _noop(*args, **kwargs):
        return None

    async def _connect(host, settings):
        return object()

    async def _reconcile(**kwargs):
        contexts.append(kwargs["ctx"])
        return SimpleNamespace(started=0, duplicates=0, held=0, deferred=0, blocked=0)

    poll_config = SimpleNamespace(reader_roles=(), allowed_fields=())
    backend = SimpleNamespace(repo_root=str(tmp_path), project="BETA", default_repo="")

    monkeypatch.setattr("scripts.config.get_settings", hermetic_settings)
    monkeypatch.setattr(
        "services.integration.tickets.resolve_ticket_backend_name", lambda settings: "rebar"
    )
    monkeypatch.setattr(
        "services.integration.jira.credentials.acceptance_field", _jira_config_missing
    )
    monkeypatch.setattr(
        "services.integration.tickets.rebar_config.load_rebar_backend_config", lambda: backend
    )
    monkeypatch.setattr(
        "services.integration.tickets.rebar_config.load_poll_config", lambda: poll_config
    )
    monkeypatch.setattr("services.temporal_client.connect", _connect)
    monkeypatch.setattr(
        "services.orchestration.temporal_activities.intake._reclaim_orphaned_slots", _noop
    )
    monkeypatch.setattr(
        "services.orchestration.intake_runtime.PostgresExecutionSeeder", lambda: object()
    )
    monkeypatch.setattr(
        "services.orchestration.intake_runtime.TemporalStarter", lambda client, settings: object()
    )
    monkeypatch.setattr(
        "services.orchestration.intake_runtime.NotificationsAlerter", lambda settings: object()
    )
    monkeypatch.setattr(
        "services.coordination.idempotency.PostgresSideEffectLedger", lambda: object()
    )
    monkeypatch.setattr("services.orchestration.intake_reconciler.build_admission", _noop)
    monkeypatch.setattr(
        "services.integration.source_control.source_control_repos", lambda settings: ()
    )
    monkeypatch.setattr(
        "services.orchestration.checks.config.required_checks_repos", lambda settings: None
    )
    monkeypatch.setattr("rebar.ready", lambda repo_root: [])
    monkeypatch.setattr("services.orchestration.rebar_intake.reconcile_rebar_intake", _reconcile)

    report = await reconcile_intake_pass(IntakeReconcileRequest())

    assert report.started == 0
    assert len(contexts) == 1


@pytest.mark.parametrize("enabled", [True, False])
async def test_the_pass_gates_on_the_model_path_only_while_the_probe_is_enabled(
    monkeypatch, tmp_path, enabled
):
    from services.orchestration.model_path import ModelPathGate

    contexts = []

    async def _noop(*args, **kwargs):
        return None

    async def _connect(host, settings):
        return object()

    async def _reconcile(**kwargs):
        contexts.append(kwargs["ctx"])
        return SimpleNamespace(started=0, duplicates=0, held=0, deferred=0, blocked=0)

    backend = SimpleNamespace(repo_root=str(tmp_path), project="BETA", default_repo="")
    monkeypatch.setattr(
        "scripts.config.get_settings", lambda: hermetic_settings(model_path_probe=enabled)
    )
    monkeypatch.setattr(
        "services.integration.tickets.resolve_ticket_backend_name", lambda settings: "rebar"
    )
    monkeypatch.setattr(
        "services.integration.tickets.rebar_config.load_rebar_backend_config", lambda: backend
    )
    monkeypatch.setattr(
        "services.integration.tickets.rebar_config.load_poll_config",
        lambda: SimpleNamespace(reader_roles=(), allowed_fields=()),
    )
    monkeypatch.setattr("services.temporal_client.connect", _connect)
    monkeypatch.setattr(
        "services.orchestration.temporal_activities.intake._reclaim_orphaned_slots", _noop
    )
    monkeypatch.setattr(
        "services.orchestration.intake_runtime.PostgresExecutionSeeder", lambda: object()
    )
    monkeypatch.setattr(
        "services.orchestration.intake_runtime.TemporalStarter", lambda client, settings: object()
    )
    monkeypatch.setattr("services.orchestration.intake_reconciler.build_admission", _noop)
    monkeypatch.setattr(
        "services.integration.source_control.source_control_repos", lambda settings: ()
    )
    monkeypatch.setattr(
        "services.orchestration.checks.config.required_checks_repos", lambda settings: None
    )
    monkeypatch.setattr("rebar.ready", lambda repo_root: [])
    monkeypatch.setattr("services.orchestration.rebar_intake.reconcile_rebar_intake", _reconcile)

    await reconcile_intake_pass(IntakeReconcileRequest())

    assert isinstance(contexts[0].model_path, ModelPathGate) is enabled
