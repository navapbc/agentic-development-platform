"""Hermetic tests for attributing model calls, traces, and cost rollups to the tenant."""

from __future__ import annotations

import contextlib
import json
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from observability import attributes as attrs
from observability import bind_execution, current_tenant, langfuse_request_metadata, tenant_tag
from observability.generation import TAGS_HEADER, langfuse_headers_from_identifiers
from observability.langfuse_read import fetch_generation_rows_by_session, observation_params
from services.orchestration.tenant_attribution import (
    TenantAttributionInterceptor,
    _AttributedActivityInbound,
    execution_id_of,
    recorded_execution_tenant,
)
from services.orchestration.usage_rollup import (
    BudgetCaps,
    UsageRow,
    build_rollup,
    build_tenant_rollups,
    read_usage_rows,
    rollup_to_dict,
    rows_by_execution,
)
from sqlalchemy.dialects import postgresql


def test_tenant_tag_follows_the_prefix_convention():
    assert tenant_tag("acme") == "tenant:acme"


def test_request_metadata_carries_the_tenant_as_trace_metadata_and_tag():
    metadata = langfuse_request_metadata(execution_id="exec-1", stage="plan", tenant="acme")
    assert metadata["trace_metadata"] == {"execution_id": "exec-1", "tenant": "acme"}
    assert "tenant:acme" in metadata["tags"]
    assert "tenant" not in langfuse_request_metadata(execution_id="exec-1")["trace_metadata"]


def test_join_headers_carry_the_tenant_tag_header():
    headers = langfuse_headers_from_identifiers(
        {attrs.EXECUTION_ID: "exec-1", attrs.TENANT: "acme"}
    )
    assert headers[TAGS_HEADER] == "tenant:acme"
    assert TAGS_HEADER not in langfuse_headers_from_identifiers({attrs.EXECUTION_ID: "exec-1"})


def test_coding_extra_body_tags_the_tenant():
    from openhands.runtime.conversation import _coding_extra_body

    body = cast(
        dict[str, Any],
        _coding_extra_body({"execution_id": "exec-5", "stage": "implementing", "tenant": "acme"}),
    )
    assert body["requestMetadata"]["tenant"] == "acme"
    assert body["metadata"]["tags"] == ["tenant:acme"]
    assert body["metadata"]["trace_metadata"] == {"execution_id": "exec-5", "tenant": "acme"}


def test_gate_engine_env_carries_the_tenant_tag_header():
    from services.orchestration.gates.engine_source import GateEngineConfig
    from services.orchestration.gates.gate_engine import _engine_env

    cfg = GateEngineConfig(
        review_entrypoint="bin/review",
        classifier_entrypoint="bin/classify",
        enabled=True,
        mode="local",
        review_model="review-model",
    )
    env = _engine_env(
        config=cfg,
        profile="cms-ars",
        gateway_base_url="http://gw",
        gateway_api_key="k",
        execution_id="exec-9",
        generation_name="ai-security-review",
        tenant="acme",
    )
    assert env["ANTHROPIC_CUSTOM_HEADERS"].splitlines() == [
        "langfuse_session_id: exec-9",
        "langfuse_generation_name: ai-security-review",
        "x-litellm-tags: tenant:acme",
    ]
    tenant_only = _engine_env(
        config=cfg,
        profile="cms-ars",
        gateway_base_url="http://gw",
        gateway_api_key="k",
        tenant="acme",
    )
    assert tenant_only["ANTHROPIC_CUSTOM_HEADERS"] == "x-litellm-tags: tenant:acme"


def test_run_key_metadata_tags_every_request_of_the_key():
    from services.integration.litellm_admin import run_key_metadata

    assert run_key_metadata("acme", "exec-1") == {
        "tenant": "acme",
        "execution_id": "exec-1",
        "tags": ["tenant:acme"],
    }


def test_execution_id_is_read_from_the_input_or_its_context():
    assert execution_id_of([SimpleNamespace(execution_id="exec-1")]) == "exec-1"
    nested = SimpleNamespace(context=SimpleNamespace(execution_id="exec-2"))
    assert execution_id_of([nested]) == "exec-2"
    assert execution_id_of([{"context": {"execution_id": "exec-3"}}]) == "exec-3"
    assert execution_id_of(["plain", 4]) == ""
    assert execution_id_of([]) == ""


async def test_the_configured_tenant_is_used_without_a_lookup():
    interceptor = TenantAttributionInterceptor(" acme ")
    assert await interceptor.tenant_for("exec-1") == "acme"
    assert await interceptor.tenant_for("") == "acme"


async def test_the_recorded_tenant_wins_and_is_cached():
    calls: list[str] = []

    async def lookup(execution_id: str) -> str:
        calls.append(execution_id)
        return {"exec-1": "acme", "exec-2": ""}.get(execution_id, "")

    interceptor = TenantAttributionInterceptor(lookup=lookup)
    assert await interceptor.tenant_for("exec-1") == "acme"
    assert await interceptor.tenant_for("exec-1") == "acme"
    assert await interceptor.tenant_for("exec-2") == ""
    assert await interceptor.tenant_for("exec-2") == ""
    assert calls == ["exec-1", "exec-2", "exec-2"]


async def test_a_worker_bound_to_a_tenant_refuses_a_run_another_tenant_recorded():
    from services.orchestration.tenant_pinning import TENANT_MISMATCH
    from temporalio.exceptions import ApplicationError

    async def lookup(execution_id: str) -> str:
        return {"exec-1": "acme", "exec-2": "globex"}.get(execution_id, "")

    interceptor = TenantAttributionInterceptor("acme", lookup=lookup)
    assert await interceptor.tenant_for("exec-1") == "acme"
    assert await interceptor.tenant_for("exec-3") == "acme"
    for _ in range(2):
        with pytest.raises(ApplicationError) as raised:
            await interceptor.tenant_for("exec-2")
        assert raised.value.type == TENANT_MISMATCH
        assert raised.value.non_retryable


async def test_a_failed_lookup_falls_back_to_the_configured_tenant():
    async def lookup(execution_id: str) -> str:
        raise RuntimeError("database unavailable")

    assert await TenantAttributionInterceptor(lookup=lookup).tenant_for("exec-1") == ""
    assert await TenantAttributionInterceptor("acme", lookup=lookup).tenant_for("exec-1") == "acme"


class _RecordingNext:
    def __init__(self) -> None:
        self.tenants: list[str] = []

    def init(self, outbound: Any) -> None:
        return None

    async def execute_activity(self, input: Any) -> str:
        self.tenants.append(current_tenant())
        return "done"


async def test_the_activity_runs_with_the_tenant_in_baggage():
    async def lookup(execution_id: str) -> str:
        return "globex"

    for interceptor, expected in (
        (TenantAttributionInterceptor("acme"), "acme"),
        (TenantAttributionInterceptor(lookup=lookup), "globex"),
        (TenantAttributionInterceptor(), ""),
    ):
        recording = _RecordingNext()
        inbound = _AttributedActivityInbound(cast(Any, recording), interceptor)
        request = SimpleNamespace(args=[SimpleNamespace(execution_id="exec-1")])
        assert await inbound.execute_activity(cast(Any, request)) == "done"
        assert recording.tenants == [expected]
    assert current_tenant() == ""


def test_the_review_worker_reads_the_recorded_tenant_and_serves_its_own():
    from services.orchestration.worker import (
        WORKER_ROLE_ACTIVITY,
        WORKER_ROLE_REVIEW,
        _tenant_attribution,
    )

    settings = SimpleNamespace(tenant="acme")
    review = _tenant_attribution(WORKER_ROLE_REVIEW, settings)
    activity = _tenant_attribution(WORKER_ROLE_ACTIVITY, settings)
    assert review._lookup is recorded_execution_tenant
    assert review._tenant == "acme"
    assert activity._lookup is None
    assert activity._tenant == "acme"


def test_bound_tenant_stamps_the_span_and_the_langfuse_trace_tag():
    from observability import configure_tracing, force_flush, get_tracer, reset_for_testing
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    reset_for_testing()
    memory = InMemorySpanExporter()
    assert configure_tracing("tenant-test", exporter=memory) is True
    try:
        with (
            bind_execution(execution_id="exec-7", tenant="acme"),
            get_tracer().start_as_current_span("gen"),
        ):
            pass
        force_flush()
        span = next(item for item in memory.get_finished_spans() if item.name == "gen")
        attributes = span.attributes or {}
        assert attributes[attrs.TENANT] == "acme"
        assert attributes[f"{attrs.LANGFUSE_TRACE_METADATA_PREFIX}tenant"] == "acme"
        assert tuple(cast(Any, attributes[attrs.LANGFUSE_TRACE_TAGS])) == ("tenant:acme",)
    finally:
        reset_for_testing()


class _Result:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> list[dict[str, Any]]:
        return self._rows

    def scalar_one_or_none(self) -> Any:
        return next(iter(self._rows[0].values())) if self._rows else None


class _Engine:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.statements: list[Any] = []

    @contextlib.asynccontextmanager
    async def begin(self):
        yield self

    connect = begin

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        return _Result(self.rows)


def _sql(statement: Any) -> str:
    return str(
        statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )


async def test_execution_tenant_reads_the_execution_record():
    from services.orchestration.temporal_activities.persistence import execution_tenant

    engine = _Engine([{"tenant": "acme"}])
    assert await execution_tenant("exec-1", engine=cast(Any, engine)) == "acme"
    assert "executions.tenant" in _sql(engine.statements[0])
    assert await execution_tenant("exec-2", engine=cast(Any, _Engine([]))) == ""
    assert await execution_tenant("exec-3", engine=cast(Any, _Engine([{"tenant": None}]))) == ""


def _usage_record(execution_id: str, stage: str, tenant: str | None, cost: float) -> dict:
    return {
        "execution_id": execution_id,
        "stage": stage,
        "task_id": "TASK-1",
        "model_id": "impl-model",
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "total_tokens": 15,
        "llm_calls": 1,
        "tool_calls": 0,
        "cost_usd": cost,
        "elapsed_seconds": 1.0,
        "execution_tenant": tenant,
    }


async def test_usage_rows_join_the_execution_tenant_and_filter_by_it():
    engine = _Engine(
        [
            _usage_record("exec-1", "IMPLEMENTING", "acme", 0.5),
            _usage_record("exec-0", "IMPLEMENTING", None, 0.1),
        ]
    )
    rows = await read_usage_rows(cast(Any, engine), tenant="acme")
    assert [row.tenant for row in rows] == ["acme", ""]
    sql = _sql(engine.statements[0])
    assert "LEFT OUTER JOIN" in sql
    assert "executions.tenant = 'acme'" in sql
    assert "usage_metrics.execution_id = '" not in sql

    await read_usage_rows(cast(Any, engine), "exec-1")
    sql = _sql(engine.statements[1])
    assert "usage_metrics.execution_id = 'exec-1'" in sql
    assert "executions.tenant = " not in sql


def _row(execution_id: str, tenant: str, cost: float, model: str = "impl-model") -> UsageRow:
    return UsageRow(
        execution_id=execution_id,
        stage="IMPLEMENTING",
        task_id=f"TASK-{execution_id}",
        model_id=model,
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        llm_calls=1,
        cost_usd=cost,
        tenant=tenant,
    )


def test_the_execution_rollup_names_its_tenant():
    tenanted = rollup_to_dict(build_rollup([_row("exec-1", "acme", 0.5)], caps=BudgetCaps()))
    untenanted = rollup_to_dict(build_rollup([_row("exec-0", "", 0.5)], caps=BudgetCaps()))
    assert tenanted["tenant"] == "acme"
    assert untenanted["tenant"] is None


def test_tenant_rollups_group_executions_and_models_per_tenant():
    rows = [
        _row("exec-1", "acme", 0.5),
        _row("exec-2", "acme", 0.25, model="review-model"),
        _row("exec-3", "globex", 1.0),
        _row("exec-0", "", 0.1),
    ]
    rollups = {item.tenant: item for item in build_tenant_rollups(rows)}
    assert list(rollups) == ["", "acme", "globex"]
    assert rollups["acme"].execution_count == 2
    assert rollups["acme"].totals.cost_usd == pytest.approx(0.75)
    assert [item.model_id for item in rollups["acme"].by_model] == ["impl-model", "review-model"]
    assert list(rows_by_execution(rows)) == ["exec-1", "exec-2", "exec-3", "exec-0"]


def test_cost_rollup_report_lists_tenants_and_executions():
    from scripts.cost_rollup import tenant_report

    grouped = rows_by_execution([_row("exec-1", "acme", 0.5), _row("exec-3", "globex", 1.0)])
    report = tenant_report(
        grouped,
        tenant="",
        caps=BudgetCaps(),
        session_url=lambda execution_id: f"http://langfuse/sessions/{execution_id}",
    )
    assert report["tenant_filter"] is None
    tenants = cast(list[dict[str, Any]], report["tenants"])
    assert [item["tenant"] for item in tenants] == ["acme", "globex"]
    assert tenants[0]["executions"] == 1
    executions = cast(list[dict[str, Any]], report["executions"])
    assert executions[0]["tenant"] == "acme"
    assert executions[0]["langfuse_session_url"] == "http://langfuse/sessions/exec-1"
    assert executions[1]["totals"]["cost_usd"] == 1.0
    json.dumps(report)


def test_reconcile_reports_the_tenant_and_a_per_tenant_summary():
    from scripts.reconcile_usage import reconcile_execution, tenant_summary

    matched = reconcile_execution(
        [_row("exec-1", "acme", 0.5)],
        [{"prompt_tokens": 10, "completion_tokens": 5, "cost_usd": 0.5}],
        caps=BudgetCaps(),
    )
    missing = reconcile_execution([_row("exec-2", "acme", 0.5)], [], caps=BudgetCaps())
    untenanted = reconcile_execution(
        [_row("exec-0", "", 0.1)],
        [{"prompt_tokens": 10, "completion_tokens": 5, "cost_usd": 0.1}],
        caps=BudgetCaps(),
    )
    assert matched["tenant"] == "acme"
    assert matched["ok"] is True
    assert missing["ok"] is False
    assert tenant_summary([matched, missing, untenanted]) == [
        {"tenant": None, "executions": 1, "mismatched": 0, "unverified": 0, "ok": True},
        {"tenant": "acme", "executions": 2, "mismatched": 1, "unverified": 0, "ok": False},
    ]


def test_an_execution_read_only_in_part_is_unverified_and_not_mismatched():
    from scripts.reconcile_usage import tenant_summary, unverified_execution

    partial = unverified_execution([_row("exec-3", "acme", 0.5)], caps=BudgetCaps())

    assert partial["unverified"] is True
    assert partial["ok"] is True
    assert tenant_summary([partial]) == [
        {"tenant": "acme", "executions": 1, "mismatched": 0, "unverified": 1, "ok": True},
    ]


def test_observation_params_filter_by_the_session_and_not_by_a_tag():
    assert "filter" not in observation_params()
    assert observation_params()["fields"] == "core,basic,usage,model"
    conditions = json.loads(observation_params("exec-1")["filter"])
    assert conditions == [
        {"type": "string", "column": "type", "operator": "=", "value": "GENERATION"},
        {"type": "string", "column": "sessionId", "operator": "=", "value": "exec-1"},
    ]


def _observations_handler(pages: dict[str, list[dict]], seen: list[httpx.QueryParams]):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params)
        session = json.loads(request.url.params["filter"])[1]["value"]
        return httpx.Response(200, json={"data": pages.get(session, []), "meta": {}})

    return handler


def test_generation_rows_are_read_per_session_with_a_session_filter():
    seen: list[httpx.QueryParams] = []
    pages = {
        "exec-1": [
            {"sessionId": "exec-1", "name": "code-review", "usageDetails": {"input": 3}},
            {"sessionId": "exec-1", "name": "implementing", "usageDetails": {"input": 4}},
        ],
    }
    client = httpx.Client(
        base_url="http://langfuse",
        transport=httpx.MockTransport(_observations_handler(pages, seen)),
    )
    read = fetch_generation_rows_by_session(
        host="http://langfuse",
        public_key="pk",
        secret_key="sk",
        session_ids=["exec-1", "exec-2"],
        client=client,
    )
    assert [row["prompt_tokens"] for row in read.rows["exec-1"]] == [3, 4]
    assert read.rows["exec-2"] == []
    assert read.incomplete == frozenset()
    assert len(seen) == 2
    assert all("tenant:" not in params["filter"] for params in seen)


def test_a_session_read_that_reaches_the_page_cap_is_incomplete():
    def handler(request: httpx.Request) -> httpx.Response:
        session = json.loads(request.url.params["filter"])[1]["value"]
        return httpx.Response(
            200,
            json={
                "data": [{"sessionId": session, "usageDetails": {"input": 1}}],
                "meta": {"cursor": "next"},
            },
        )

    client = httpx.Client(base_url="http://langfuse", transport=httpx.MockTransport(handler))
    read = fetch_generation_rows_by_session(
        host="http://langfuse",
        public_key="pk",
        secret_key="sk",
        session_ids=["exec-1"],
        page_cap=2,
        client=client,
    )
    assert len(read.rows["exec-1"]) == 2
    assert read.incomplete == frozenset({"exec-1"})
