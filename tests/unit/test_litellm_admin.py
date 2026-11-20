"""Tests for the LiteLLM admin client and the per-run virtual keys it issues."""

from __future__ import annotations

import json

import httpx
import pytest
from services.integration import litellm_admin
from services.integration.litellm_admin import (
    RUN_MODEL_BUDGET_EXCEEDED,
    TENANT_MODEL_BUDGET_EXCEEDED,
    LiteLLMAdminClient,
    LiteLLMAdminError,
    RunModelAccessError,
    TeamLimits,
    classify_budget_refusal,
    derive_run_model_key,
    ensure_run_model_key,
    ensure_tenant_team,
    revoke_run_model_key,
    run_model_budget_stop,
)
from tests.support import hermetic_settings

_MASTER = "sk-master-secret"


class _FakeLiteLLM:
    """An in-memory LiteLLM admin API that answers like the v1.109 proxy with a database."""

    def __init__(self) -> None:
        self.keys: dict[str, dict] = {}
        self.deleted: dict[str, dict] = {}
        self.teams: dict[str, dict] = {}
        self.calls: list[tuple[str, str, dict]] = []
        self.failures: list[int] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {_MASTER}"
        body = json.loads(request.content) if request.content else {}
        params = dict(request.url.params)
        self.calls.append((request.method, request.url.path, body or params))
        if self.failures:
            return httpx.Response(self.failures.pop(0), json={"error": "busy"})
        route = (request.method, request.url.path)
        if route == ("POST", "/v2/key/info"):
            assert not request.url.query
            keys = body["keys"]
            found = [self.keys[key] for key in keys if key in self.keys]
            return httpx.Response(200, json={"key": keys, "info": found})
        if route == ("POST", "/key/generate"):
            if body["key"] in self.keys:
                return httpx.Response(400, json={"error": "duplicate key"})
            self.keys[body["key"]] = {**body, "spend": 0.0, "status": "active"}
            return httpx.Response(200, json={"key": body["key"]})
        if route == ("POST", "/key/update"):
            self.keys[body["key"]].update(body)
            return httpx.Response(200, json=body)
        if route == ("POST", "/key/delete"):
            key = body["keys"][0]
            if key not in self.keys:
                return httpx.Response(404, json={"detail": f"no key {key}"})
            self.deleted[key] = self.keys.pop(key)
            return httpx.Response(200, json={"deleted_keys": [key]})
        if route == ("GET", "/team/info"):
            team = self.teams.get(params["team_id"])
            if team is None:
                return httpx.Response(404, json={"detail": "team not found"})
            return httpx.Response(200, json={"team_id": params["team_id"], "team_info": team})
        if route == ("POST", "/team/new"):
            if body["team_id"] in self.teams:
                return httpx.Response(400, json={"error": "team already exists"})
            self.teams[body["team_id"]] = {**body, "spend": 0.0}
            return httpx.Response(200, json=body)
        if route == ("POST", "/team/update"):
            self.teams[body["team_id"]].update(body)
            return httpx.Response(200, json=body)
        return httpx.Response(404, json={"detail": "no route"})

    def routes(self) -> list[tuple[str, str]]:
        return [(method, path) for method, path, _ in self.calls]


async def _no_sleep(seconds: float) -> None:
    return None


@pytest.fixture
def server() -> _FakeLiteLLM:
    return _FakeLiteLLM()


@pytest.fixture
def client(server: _FakeLiteLLM) -> LiteLLMAdminClient:
    return LiteLLMAdminClient(
        "http://litellm:4000",
        _MASTER,
        transport=httpx.MockTransport(server.handle),
        sleep=_no_sleep,
    )


@pytest.fixture(autouse=True)
def _fresh_team_cache(monkeypatch):
    monkeypatch.setattr(litellm_admin, "_team_ready_until", {})


def _settings(**overrides):
    values = {
        "tenant": "acme",
        "litellm_master_key": _MASTER,
        "run_model_budget_usd": 4.5,
        "run_model_access_ttl_seconds": 3600,
    }
    values.update(overrides)
    return hermetic_settings(**values)


def test_the_run_key_is_stable_per_execution_and_distinct_across_executions():
    first = derive_run_model_key(_MASTER, "exec-1")

    assert first == derive_run_model_key(_MASTER, "exec-1")
    assert first != derive_run_model_key(_MASTER, "exec-2")
    assert first != derive_run_model_key("sk-other-master", "exec-1")
    assert first.startswith("sk-run-")
    assert len(first) >= 16
    assert first != _MASTER
    assert _MASTER not in first


async def test_a_new_run_key_is_issued_under_the_tenant_team(server, client):
    key = await ensure_run_model_key("exec-1", settings=_settings(), client=client)

    assert key == derive_run_model_key(_MASTER, "exec-1")
    issued = server.keys[key]
    assert issued["team_id"] == "acme"
    assert issued["key_alias"] == "run-exec-1"
    assert issued["max_budget"] == 4.5
    assert issued["duration"] == "3600s"
    assert issued["metadata"] == {
        "tenant": "acme",
        "execution_id": "exec-1",
        "tags": ["tenant:acme"],
    }
    assert server.teams["acme"]["team_alias"] == "acme"


async def test_an_existing_run_key_has_its_expiry_renewed(server, client):
    settings = _settings()
    key = await ensure_run_model_key("exec-1", settings=settings, client=client)
    server.calls.clear()

    again = await ensure_run_model_key("exec-1", settings=settings, client=client)

    assert again == key
    assert ("POST", "/key/generate") not in server.routes()
    assert ("POST", "/key/update", {"key": key, "duration": "3600s"}) in server.calls


async def test_a_deleted_run_key_is_issued_again(server, client):
    settings = _settings()
    key = await ensure_run_model_key("exec-1", settings=settings, client=client)
    assert await revoke_run_model_key("exec-1", settings=settings, client=client) is True

    await ensure_run_model_key("exec-1", settings=settings, client=client)

    assert server.keys[key]["status"] == "active"


async def test_deleting_a_key_that_is_gone_succeeds(server, client):
    assert await revoke_run_model_key("exec-9", settings=_settings(), client=client) is False


async def test_a_run_without_a_tenant_is_refused_a_key(server, client):
    with pytest.raises(RunModelAccessError, match="TENANT"):
        await ensure_run_model_key("exec-1", settings=_settings(tenant=""), client=client)

    assert server.calls == []


async def test_a_run_without_an_execution_id_is_refused_a_key(server, client):
    with pytest.raises(RunModelAccessError):
        await ensure_run_model_key("", settings=_settings(), client=client)


async def test_busy_answers_are_retried(server, client):
    server.failures = [429, 503]

    assert await client.team_info("acme") is None
    assert len(server.calls) == 3


async def test_retries_give_up_with_an_error(server, client):
    server.failures = [503, 503, 503]

    with pytest.raises(LiteLLMAdminError) as failed:
        await client.team_info("acme")

    assert failed.value.status == 503


async def test_an_unreachable_proxy_is_retried_then_reported():
    attempts: list[str] = []

    def refuse(request: httpx.Request) -> httpx.Response:
        attempts.append(request.url.path)
        raise httpx.ConnectError("connection refused", request=request)

    client = LiteLLMAdminClient(
        "http://litellm:4000", _MASTER, transport=httpx.MockTransport(refuse), sleep=_no_sleep
    )

    with pytest.raises(LiteLLMAdminError, match="could not be reached"):
        await client.key_info("sk-run-x")
    assert len(attempts) == 3


async def test_the_key_lookup_carries_the_key_in_the_body_and_not_the_url(server, client):
    key = await ensure_run_model_key("exec-1", settings=_settings(), client=client)
    server.calls.clear()

    info = await client.key_info(key)

    assert info is not None
    assert info["key_alias"] == "run-exec-1"
    assert server.calls == [("POST", "/v2/key/info", {"keys": [key]})]


async def test_an_unknown_key_reads_as_absent(server, client):
    assert await client.key_info("sk-run-unknown-value") is None


async def test_errors_never_carry_the_master_key_or_the_run_key():
    def echo(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text=f"bad {_MASTER} {request.content.decode()}")

    client = LiteLLMAdminClient(
        "http://litellm:4000", _MASTER, transport=httpx.MockTransport(echo), sleep=_no_sleep
    )

    with pytest.raises(LiteLLMAdminError) as failed:
        await client.update_key(key="sk-run-secret-value", duration="60s")

    assert _MASTER not in str(failed.value)
    assert "sk-run-secret-value" not in str(failed.value)


async def test_the_team_is_created_with_the_configured_limits(server, client):
    limits = TeamLimits(max_budget=100.0, budget_duration="30d", rpm_limit=60, tpm_limit=0)

    await ensure_tenant_team(client, "acme", limits)

    team = server.teams["acme"]
    assert team["max_budget"] == 100.0
    assert team["budget_duration"] == "30d"
    assert team["rpm_limit"] == 60
    assert "tpm_limit" not in team


async def test_an_existing_team_is_brought_to_the_configured_limits(server, client):
    server.teams["acme"] = {"team_id": "acme", "max_budget": 5.0, "spend": 0.0}

    await ensure_tenant_team(client, "acme", TeamLimits(max_budget=50.0, budget_duration="7d"))

    assert server.teams["acme"]["max_budget"] == 50.0
    assert ("POST", "/team/new") not in server.routes()


def _team_updates(server: _FakeLiteLLM) -> list[dict]:
    return [body for method, path, body in server.calls if path == "/team/update"]


async def test_a_team_already_at_the_configured_limits_is_not_updated(server, client):
    server.teams["acme"] = {
        "team_id": "acme",
        "max_budget": 100.0,
        "budget_duration": "30d",
        "rpm_limit": 60,
        "tpm_limit": None,
        "spend": 0.0,
    }

    await ensure_tenant_team(
        client, "acme", TeamLimits(max_budget=100.0, budget_duration="30d", rpm_limit=60)
    )

    assert _team_updates(server) == []


async def test_a_changed_budget_is_sent_without_the_unchanged_duration(server, client):
    server.teams["acme"] = {
        "team_id": "acme",
        "max_budget": 100.0,
        "budget_duration": "30d",
        "spend": 0.0,
    }

    await ensure_tenant_team(client, "acme", TeamLimits(max_budget=150.0, budget_duration="30d"))

    assert _team_updates(server) == [{"team_id": "acme", "max_budget": 150.0}]


async def test_a_budget_set_back_to_zero_clears_the_budget_and_its_duration(server, client):
    server.teams["acme"] = {
        "team_id": "acme",
        "max_budget": 100.0,
        "budget_duration": "30d",
        "rpm_limit": 60,
        "spend": 0.0,
    }

    await ensure_tenant_team(client, "acme", TeamLimits(budget_duration="30d", rpm_limit=60))

    assert _team_updates(server) == [
        {"team_id": "acme", "max_budget": None, "budget_duration": None}
    ]
    assert server.teams["acme"]["max_budget"] is None
    assert server.teams["acme"]["budget_duration"] is None


async def test_a_rate_limit_set_back_to_zero_is_cleared(server, client):
    server.teams["acme"] = {"team_id": "acme", "rpm_limit": 60, "tpm_limit": 1000, "spend": 0.0}

    await ensure_tenant_team(client, "acme", TeamLimits(tpm_limit=1000))

    assert _team_updates(server) == [{"team_id": "acme", "rpm_limit": None}]


async def test_a_team_created_by_another_worker_gets_only_the_differing_fields(server, client):
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/team/new":
            server.teams["acme"] = {
                "team_id": "acme",
                "max_budget": 20.0,
                "budget_duration": "30d",
                "spend": 0.0,
            }
        return server.handle(request)

    racing = LiteLLMAdminClient(
        "http://litellm:4000", _MASTER, transport=httpx.MockTransport(handle), sleep=_no_sleep
    )

    await ensure_tenant_team(
        racing, "acme", TeamLimits(max_budget=20.0, budget_duration="30d", rpm_limit=30)
    )

    assert _team_updates(server) == [{"team_id": "acme", "rpm_limit": 30}]


async def test_the_team_check_is_cached_for_a_while(server, client):
    now = [1000.0]
    limits = TeamLimits()

    await ensure_tenant_team(client, "acme", limits, clock=lambda: now[0])
    await ensure_tenant_team(client, "acme", limits, clock=lambda: now[0])
    assert server.routes().count(("GET", "/team/info")) == 1

    now[0] += 600
    await ensure_tenant_team(client, "acme", limits, clock=lambda: now[0])
    assert server.routes().count(("GET", "/team/info")) == 2


async def test_a_spent_run_key_is_a_run_budget_stop(server, client):
    settings = _settings()
    key = await ensure_run_model_key("exec-1", settings=settings, client=client)
    server.keys[key]["spend"] = 4.5

    reason = await run_model_budget_stop("exec-1", settings=settings, client=client)

    assert reason == RUN_MODEL_BUDGET_EXCEEDED


async def test_a_spent_team_is_a_tenant_budget_stop(server, client):
    settings = _settings(tenant_model_budget_usd=20.0)
    await ensure_run_model_key("exec-1", settings=settings, client=client)
    server.teams["acme"]["spend"] = 20.01

    reason = await run_model_budget_stop("exec-1", settings=settings, client=client)

    assert reason == TENANT_MODEL_BUDGET_EXCEEDED


class _RecordedSleep:
    def __init__(self, on_sleep=None) -> None:
        self.slept: list[float] = []
        self._on_sleep = on_sleep

    async def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)
        if self._on_sleep is not None:
            self._on_sleep(len(self.slept))


async def test_spend_that_lands_after_the_refusal_is_read_on_a_later_attempt(server, client):
    settings = _settings()
    key = await ensure_run_model_key("exec-1", settings=settings, client=client)
    server.calls.clear()

    def batch_write(sleeps: int) -> None:
        if sleeps == 2:
            server.keys[key]["spend"] = 4.5

    sleep = _RecordedSleep(batch_write)

    reason = await run_model_budget_stop("exec-1", settings=settings, client=client, sleep=sleep)

    assert reason == RUN_MODEL_BUDGET_EXCEEDED
    assert sleep.slept == [5.0, 5.0]
    assert server.routes().count(("POST", "/v2/key/info")) == 3


async def test_budget_left_on_key_and_team_is_no_budget_stop(server, client):
    settings = _settings(tenant_model_budget_usd=20.0)
    await ensure_run_model_key("exec-1", settings=settings, client=client)
    server.calls.clear()
    sleep = _RecordedSleep()

    reason = await run_model_budget_stop("exec-1", settings=settings, client=client, sleep=sleep)

    assert reason == ""
    assert sleep.slept == [5.0, 5.0, 5.0, 5.0]
    assert server.routes().count(("POST", "/v2/key/info")) == 5
    assert server.routes().count(("GET", "/team/info")) == 5


async def test_an_unreadable_budget_is_no_budget_stop(server, client):
    server.failures = [500, 500, 500]
    sleep = _RecordedSleep()

    reason = await run_model_budget_stop("exec-1", settings=_settings(), client=client, sleep=sleep)

    assert reason == ""
    assert sleep.slept == []
    assert len(server.calls) == 3


@pytest.mark.parametrize(
    ("failure_text", "reason"),
    [
        (
            "litellm.BadRequestError: Litellm_proxyException - Budget has been exceeded! "
            "Key=run-exec-1 (sk-...) Current cost: 0.1113832, Max budget: 0.005",
            RUN_MODEL_BUDGET_EXCEEDED,
        ),
        (
            "Budget has been exceeded! Key=run-exec-1 Current cost: 0.0024, "
            "Estimated request cost: 0.109, Max budget: 0.005",
            RUN_MODEL_BUDGET_EXCEEDED,
        ),
        (
            "ExceededBudget: Key over 1d budget. Spend=$0.0100, Limit=$0.01",
            RUN_MODEL_BUDGET_EXCEEDED,
        ),
        (
            "Budget has been exceeded! Team=acme Current cost: 20.5, Max budget: 20.0",
            TENANT_MODEL_BUDGET_EXCEEDED,
        ),
        (
            "Budget has been exceeded! Team=acme Current cost: 19.9, "
            "Estimated request cost: 0.4, Max budget: 20.0",
            TENANT_MODEL_BUDGET_EXCEEDED,
        ),
        (
            "ExceededBudget: Team=acme over 30d budget. Spend=$20.0000, Limit=$20.00",
            TENANT_MODEL_BUDGET_EXCEEDED,
        ),
        (
            "Budget has been exceeded! User=u-1 in Team=acme Current cost: 3.0, Max budget: 2.0",
            "",
        ),
        ("Budget has been exceeded! TeamMember=u-1 Current cost: 3.0, Max budget: 2.0", ""),
        ("ExceededBudget: User=u-1 over budget. Spend=3.0, Budget=2.0", ""),
        ("Error code: 422 - {'detail': 'Unprocessable Entity'}", ""),
        ("", ""),
    ],
)
def test_a_refusal_message_names_the_budget(failure_text, reason):
    assert classify_budget_refusal(failure_text) == reason


async def test_a_refusal_message_decides_without_reading_litellm(server, client):
    sleep = _RecordedSleep()

    reason = await run_model_budget_stop(
        "exec-1",
        failure_text="Budget has been exceeded! Team=acme Current cost: 1.0, Max budget: 0.5",
        settings=_settings(),
        client=client,
        sleep=sleep,
    )

    assert reason == TENANT_MODEL_BUDGET_EXCEEDED
    assert server.calls == []
    assert sleep.slept == []


async def test_an_unrelated_failure_text_falls_back_to_the_spend_reads(server, client):
    settings = _settings()
    key = await ensure_run_model_key("exec-1", settings=settings, client=client)
    server.keys[key]["spend"] = 4.5
    server.calls.clear()

    reason = await run_model_budget_stop(
        "exec-1",
        failure_text="sandbox pod entered terminal phase Failed",
        settings=settings,
        client=client,
        sleep=_RecordedSleep(),
    )

    assert reason == RUN_MODEL_BUDGET_EXCEEDED
    assert ("POST", "/v2/key/info") in server.routes()


async def test_run_model_access_passes_the_failure_text_on(monkeypatch):
    received: list[str] = []

    async def budget_stop(execution_id, *, failure_text="", settings=None):
        received.append(failure_text)
        return RUN_MODEL_BUDGET_EXCEEDED

    monkeypatch.setattr(litellm_admin, "run_model_budget_stop", budget_stop)

    reason = await litellm_admin.RunModelAccess().budget_stop("exec-1", failure_text="refused")

    assert reason == RUN_MODEL_BUDGET_EXCEEDED
    assert received == ["refused"]
