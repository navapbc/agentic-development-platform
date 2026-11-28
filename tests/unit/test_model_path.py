from __future__ import annotations

import json

import httpx
import pytest
from services.orchestration.model_path import ModelPathHealth, ModelPathProbe, probe_model_path
from tests.support import hermetic_settings


def _answering(status: int, text: str = "", requests: list | None = None):
    def _handle(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        return httpx.Response(status, text=text)

    return httpx.MockTransport(_handle)


def _raising(error: Exception):
    def _handle(request: httpx.Request) -> httpx.Response:
        raise error

    return httpx.MockTransport(_handle)


async def test_the_probe_asks_the_planning_model_for_one_token():
    requests: list[httpx.Request] = []

    health = await probe_model_path(
        hermetic_settings(litellm_master_key="sk-probe"),
        transport=_answering(200, "{}", requests),
    )

    assert health.healthy
    body = json.loads(requests[0].content)
    assert (body["model"], body["max_tokens"]) == ("planning-model", 1)
    assert requests[0].headers["Authorization"] == "Bearer sk-probe"


@pytest.mark.parametrize(
    ("transport", "hop"),
    [
        (
            _answering(500, "litellm.APIConnectionError: refreshed credentials are still expired"),
            "credentials",
        ),
        (
            _answering(
                500,
                "BedrockException - Cannot connect to host bedrock-runtime.us-east-1.amazonaws.com",
            ),
            "bedrock_path",
        ),
        (_raising(httpx.ConnectError("[Errno 61] Connection refused")), "litellm"),
        (_raising(httpx.ReadTimeout("timed out")), "litellm"),
    ],
)
async def test_the_probe_names_the_failing_hop(transport, hop):
    health = await probe_model_path(hermetic_settings(), transport=transport)

    assert health.hop == hop
    assert health.error


async def test_a_failure_the_probe_cannot_attribute_does_not_defer_intake():
    health = await probe_model_path(
        hermetic_settings(), transport=_answering(429, "litellm.RateLimitError")
    )

    assert health.healthy
    assert "429" in health.error


async def test_the_probe_answer_is_reused_until_the_cache_expires():
    now = [0.0]
    sent: list[int] = []

    async def _probe() -> ModelPathHealth:
        sent.append(1)
        return ModelPathHealth(hop="credentials")

    probe = ModelPathProbe(_probe, ttl=50.0, clock=lambda: now[0])

    first = [await probe.check() for _ in range(3)]
    now[0] = 50.0
    later = await probe.check()

    assert [fresh for _, fresh in first] == [True, False, False]
    assert later[1] is True
    assert len(sent) == 2


class _Parks:
    def __init__(self, parked: dict[str, list[str]]) -> None:
        self._parked = parked
        self.resumed: list[str] = []

    async def parked(self, tenant: str) -> list[str]:
        return [wid for wid in self._parked.get(tenant, []) if wid not in self.resumed]

    async def resume(self, workflow_id: str) -> bool:
        self.resumed.append(workflow_id)
        return True


async def test_a_recovered_model_path_resumes_only_the_tenant_s_parked_runs(monkeypatch):
    from services.coordination.idempotency import InMemorySideEffectLedger
    from tests.support import ModelPathStub, capture_alerts, model_path_gate

    alerts = capture_alerts(monkeypatch)
    ledger = InMemorySideEffectLedger()
    parks = _Parks({"acme": ["draft-pr-ACME-1"], "beta": ["draft-pr-BETA-1"]})
    path = ModelPathStub("bedrock_path")
    gate = model_path_gate(
        path, ledger=ledger, settings=hermetic_settings(tenant="acme"), parks=parks
    )

    await gate.sweep()
    path.hop = ""
    path.next_pass()
    await gate.sweep()
    path.next_pass()
    await gate.sweep()

    assert parks.resumed == ["draft-pr-ACME-1"]
    assert [alert.subject for alert in alerts] == [
        "Model path unavailable for tenant acme, Bedrock connection (bedrock_path hop)",
        "Model path recovered for tenant acme",
    ]
    assert [alert.issue_key for alert in alerts] == ["", ""]
    assert "first deferred ticket" not in alerts[0].body
    assert path.probes == 2


async def test_a_pass_with_no_parked_run_and_no_candidate_still_sends_no_probe():
    from services.coordination.idempotency import InMemorySideEffectLedger
    from tests.support import ModelPathStub, model_path_gate

    path = ModelPathStub("")
    gate = model_path_gate(path, ledger=InMemorySideEffectLedger(), parks=_Parks({}))

    await gate.sweep()

    assert path.probes == 0


async def test_parked_runs_are_read_for_one_tenant_and_the_model_unavailable_reason():
    from types import SimpleNamespace

    from services.orchestration.model_path_parks import PostgresParkedRuns
    from sqlalchemy.dialects import postgresql

    statements: list = []

    class _Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        async def execute(self, statement):
            statements.append(statement)
            return SimpleNamespace(all=lambda: [SimpleNamespace(workflow_id="draft-pr-A-1")])

    engine = SimpleNamespace(connect=_Connection)

    found = await PostgresParkedRuns(engine).find("acme", issue_key="A-1")

    sql = str(
        statements[0].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )
    assert found == ["draft-pr-A-1"]
    assert "suspended_flows.reason = 'model_unavailable'" in sql
    assert "executions.tenant = 'acme'" in sql
    assert "executions.state = 'AWAITING_HUMAN'" in sql
    assert "executions.task_id = 'A-1'" in sql


class _TenantParkedRuns:
    def __init__(self) -> None:
        self.queries: list[tuple[str, str, str]] = []

    async def find(self, tenant: str, *, issue_key: str = "", workflow_id: str = "") -> list[str]:
        self.queries.append((tenant, issue_key, workflow_id))
        if tenant == "acme" and (issue_key == "T-1" or workflow_id == "draft-pr-T-1-exec-1"):
            return ["draft-pr-T-1-exec-1"]
        return []


class _ResumeOnce:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def resume(self, workflow_id: str, *, actor: str, actor_type: str) -> bool:
        self.calls.append((workflow_id, actor, actor_type))
        return len(self.calls) == 1


def test_resume_parked_resumes_one_model_path_park_by_issue_or_workflow_id(monkeypatch, capsys):
    import sys

    import scripts.resume_parked as script
    from services.orchestration import model_path_parks

    runs, resumer = _TenantParkedRuns(), _ResumeOnce()

    async def _resume(*, issue_key: str = "", workflow_id: str = "", actor: str, settings=None):
        return await model_path_parks.resume_parked_runs(
            tenant="acme",
            issue_key=issue_key,
            workflow_id=workflow_id,
            actor=actor,
            runs=runs,
            resumer=resumer,
        )

    monkeypatch.setattr(model_path_parks, "run_resume_parked_runs", _resume)
    monkeypatch.setattr("scripts.operator_identity.local_operator", lambda: "ops")
    codes = []
    for target in (
        ["--issue", "T-1"],
        ["--workflow-id", "draft-pr-T-1-exec-1"],
        ["--issue", "T-2"],
    ):
        monkeypatch.setattr(sys, "argv", ["resume_parked", *target])
        codes.append(script.cli())

    assert codes == [0, 1, 1]
    assert capsys.readouterr().out.splitlines() == [
        "[resume-parked] T-1 resumed draft-pr-T-1-exec-1",
        "[resume-parked] draft-pr-T-1-exec-1 has already left the model path park on "
        "draft-pr-T-1-exec-1",
        "[resume-parked] T-2 has no run parked on the model path, nothing to resume",
    ]
    assert runs.queries == [
        ("acme", "T-1", ""),
        ("acme", "", "draft-pr-T-1-exec-1"),
        ("acme", "T-2", ""),
    ]
    assert resumer.calls == [("draft-pr-T-1-exec-1", "ops", "operator")] * 2


async def test_an_intake_recovery_edits_the_outage_post_and_replies_in_its_thread(monkeypatch):
    from services.coordination.idempotency import InMemorySideEffectLedger
    from services.orchestration.notifications import SlackPostRef
    from tests.support import InMemoryModelPathEpisodes, ModelPathStub, model_path_gate

    alerts, edits = [], []

    def _deliver(message, settings=None):
        alerts.append(message)
        if not message.thread_ts:
            message.posts.append(SlackPostRef("C0123456789", "100.1"))
        return ["slack"]

    monkeypatch.setattr("services.orchestration.notifications.deliver_alert", _deliver)
    monkeypatch.setattr(
        "services.orchestration.notifications.update_slack_post",
        lambda post, **kwargs: edits.append((post.ts, kwargs["blocks"])) or True,
    )
    episodes = InMemoryModelPathEpisodes()
    path = ModelPathStub("credentials")
    settings = hermetic_settings(tenant="acme", escalation_action_key="key")
    gate = model_path_gate(
        path, ledger=InMemorySideEffectLedger(), settings=settings, episodes=episodes
    )

    await gate.admit(issue_key="ACME-1")
    assert episodes.open_episodes["acme"].slack_ts == "100.1"
    [actions] = [block for block in alerts[0].slack_blocks if block["type"] == "actions"]
    assert [button["action_id"] for button in actions["elements"]] == [
        "acknowledge",
        "validate",
        "fix_done",
    ]
    path.hop = ""
    path.next_pass()
    await gate.admit(issue_key="ACME-2")

    assert alerts[1].thread_ts == "100.1"
    [(ts, blocks)] = edits
    assert ts == "100.1"
    assert blocks[-1]["block_id"] == "adp-resolved"
    assert "Intake found the path healthy." in blocks[-1]["elements"][0]["text"]
    assert not any(block["type"] == "actions" for block in blocks)


async def test_an_intake_resolution_keeps_every_line_of_the_outage_post_and_drops_its_buttons(
    monkeypatch,
):
    from services.coordination.idempotency import InMemorySideEffectLedger
    from services.orchestration import notifications
    from tests.support import InMemoryModelPathEpisodes, ModelPathStub, model_path_gate

    alerts, edits = [], []

    def _deliver(message, settings=None):
        alerts.append(message)
        notifications._record_post(message, "C0123456789", "100.1", blocks=message.slack_blocks)
        return ["slack"]

    monkeypatch.setattr("services.orchestration.notifications.deliver_alert", _deliver)
    monkeypatch.setattr(
        "services.orchestration.notifications.update_slack_post",
        lambda post, **kwargs: edits.append((post.ts, kwargs["blocks"])) or True,
    )
    episodes = InMemoryModelPathEpisodes()
    path = ModelPathStub("credentials")
    settings = hermetic_settings(tenant="acme", escalation_action_key="key")
    gate = model_path_gate(
        path, ledger=InMemorySideEffectLedger(), settings=settings, episodes=episodes
    )

    await gate.admit(issue_key="ACME-1")
    original = [block for block in alerts[0].slack_blocks if block["type"] != "actions"]
    path.hop = ""
    path.next_pass()
    await gate.admit(issue_key="ACME-2")

    [(ts, blocks)] = edits
    assert ts == "100.1"
    assert blocks[:-1] == original
    assert "What happened" in json.dumps(original) and "Status" in json.dumps(original)
    assert blocks[-1]["block_id"] == "adp-resolved"
    assert blocks[-1]["elements"][0]["text"].endswith("Intake found the path healthy.")


def test_the_probe_cache_expires_within_one_default_intake_interval() -> None:
    from scripts.config import Settings
    from services.orchestration.model_path import PROBE_CACHE_SECONDS

    intake_interval = Settings.model_fields["intake_schedule_interval_seconds"].default
    assert intake_interval > PROBE_CACHE_SECONDS
