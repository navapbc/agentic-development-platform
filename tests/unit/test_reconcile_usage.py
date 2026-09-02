"""Hermetic tests for reading and reconciling Langfuse generations."""

from __future__ import annotations

import json

import httpx
from observability import reconcile_usage
from observability.langfuse_read import fetch_session_generations, generation_usage


def test_generation_usage_reads_usage_and_cost_details():
    observation = {
        "usageDetails": {"input": 100, "output": 40},
        "costDetails": {"total": 0.25},
    }
    assert generation_usage(observation) == {
        "prompt_tokens": 100,
        "completion_tokens": 40,
        "cost_usd": 0.25,
    }


def test_generation_usage_falls_back_to_flat_fields():
    observation = {"promptTokens": 10, "completionTokens": 5, "totalPrice": 0.01}
    usage = generation_usage(observation)
    assert usage["prompt_tokens"] == 10
    assert usage["completion_tokens"] == 5
    assert usage["cost_usd"] == 0.01


def test_generation_usage_defaults_to_zero():
    assert generation_usage({}) == {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0}


def _paged_handler(pages: list[dict]):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        index = calls["n"]
        calls["n"] += 1
        page = pages[index] if index < len(pages) else {"data": [], "meta": {}}
        return httpx.Response(200, json=page)

    return handler


def test_fetch_filters_by_session_and_pages():
    pages = [
        {
            "data": [
                {"sessionId": "exec-1", "usageDetails": {"input": 100, "output": 40},
                 "costDetails": {"total": 0.20}},
                {"sessionId": "other", "usageDetails": {"input": 999, "output": 999},
                 "costDetails": {"total": 9.99}},
            ],
            "meta": {"cursor": "next"},
        },
        {
            "data": [
                {"sessionId": "exec-1", "usageDetails": {"input": 500, "output": 300},
                 "costDetails": {"total": 1.50}},
            ],
            "meta": {},
        },
    ]
    transport = httpx.MockTransport(_paged_handler(pages))
    client = httpx.Client(base_url="http://langfuse", transport=transport)

    generations = fetch_session_generations(
        host="http://langfuse",
        public_key="pk",
        secret_key="sk",
        session_id="exec-1",
        client=client,
    )
    client.close()

    assert len(generations) == 2
    assert generations[0]["prompt_tokens"] == 100
    assert generations[1]["completion_tokens"] == 300

    reconciliation = reconcile_usage(
        generations, prompt_tokens=600, completion_tokens=340, cost_usd=1.70, cost_tolerance=0.01
    )
    assert reconciliation.ok is True


def test_fetch_reports_drift_against_usage_metrics():
    pages = [
        {
            "data": [
                {"sessionId": "exec-2", "usageDetails": {"input": 100, "output": 40},
                 "costDetails": {"total": 0.20}},
            ],
            "meta": {},
        }
    ]
    transport = httpx.MockTransport(_paged_handler(pages))
    client = httpx.Client(base_url="http://langfuse", transport=transport)
    generations = fetch_session_generations(
        host="http://langfuse",
        public_key="pk",
        secret_key="sk",
        session_id="exec-2",
        client=client,
    )
    client.close()

    reconciliation = reconcile_usage(
        generations, prompt_tokens=999, completion_tokens=40, cost_usd=0.20, cost_tolerance=0.01
    )
    assert reconciliation.ok is False
    assert reconciliation.prompt_token_diff == 100 - 999


def test_response_shape_is_json_serializable():
    usage = generation_usage(
        {"usageDetails": {"input": 1, "output": 2}, "costDetails": {"total": 0.001}}
    )
    assert json.loads(json.dumps(usage)) == usage


def test_generation_row_carries_stage_and_model():
    from observability.langfuse_read import generation_row

    observation = {
        "name": "code-review",
        "model": "review-model",
        "sessionId": "exec-1",
        "usageDetails": {"input": 200, "output": 60},
        "costDetails": {"total": 0.12},
    }
    row = generation_row(observation)
    assert row["stage"] == "code-review"
    assert row["model_id"] == "review-model"
    assert row["prompt_tokens"] == 200
    assert row["completion_tokens"] == 60
    assert row["cost_usd"] == 0.12


def test_fetch_generation_rows_filters_by_session():
    from observability.langfuse_read import fetch_session_generation_rows

    pages = [
        {
            "data": [
                {"sessionId": "exec-9", "name": "plan-review", "model": "review-model",
                 "usageDetails": {"input": 10, "output": 5}, "costDetails": {"total": 0.01}},
                {"sessionId": "other", "name": "plan-review", "model": "review-model",
                 "usageDetails": {"input": 999, "output": 999}, "costDetails": {"total": 9.9}},
            ],
            "meta": {},
        }
    ]
    transport = httpx.MockTransport(_paged_handler(pages))
    client = httpx.Client(base_url="http://langfuse", transport=transport)
    rows = fetch_session_generation_rows(
        host="http://langfuse", public_key="pk", secret_key="sk",
        session_id="exec-9", client=client,
    )
    client.close()
    assert len(rows) == 1
    assert rows[0]["stage"] == "plan-review"
    assert rows[0]["prompt_tokens"] == 10
