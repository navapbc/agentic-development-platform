"""Hermetic tests for LLM gateway Langfuse metadata and usage reconciliation."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from observability import (
    attributes as attrs,
)
from observability import (
    configure_tracing,
    force_flush,
    langfuse_request_metadata,
    reconcile_usage,
    reset_for_testing,
)
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

_LITELLM_CONFIG = (
    Path(__file__).resolve().parents[2] / "config" / "local" / "litellm-config.yaml"
)


def test_metadata_carries_execution_stage_and_version() -> None:
    metadata = langfuse_request_metadata(
        execution_id="exec-1",
        task_id="TASK-1",
        stage="plan",
        prompt_version="v3",
    )
    assert metadata["generation_name"] == "plan"
    assert metadata["version"] == "v3"
    assert metadata["session_id"] == "exec-1"
    assert metadata["trace_metadata"] == {"execution_id": "exec-1", "task_id": "TASK-1"}
    assert "execution_id:exec-1" in metadata["tags"]
    assert "stage:plan" in metadata["tags"]


def test_metadata_is_empty_without_identifiers() -> None:
    assert langfuse_request_metadata() == {}


def test_reconcile_usage_matches_and_reports_diffs() -> None:
    generations = [
        {"prompt_tokens": 10, "completion_tokens": 5, "cost_usd": 0.01},
        {"prompt_tokens": 20, "completion_tokens": 7, "cost_usd": 0.02},
    ]
    ok = reconcile_usage(
        generations, prompt_tokens=30, completion_tokens=12, cost_usd=0.03
    )
    assert ok.ok is True

    off = reconcile_usage(
        generations, prompt_tokens=29, completion_tokens=12, cost_usd=0.03
    )
    assert off.ok is False
    assert off.prompt_token_diff == 1


def test_litellm_config_enables_the_langfuse_callback() -> None:
    config = yaml.safe_load(_LITELLM_CONFIG.read_text())
    assert "langfuse_otel" in config["litellm_settings"]["callbacks"]


class _FakeResponse:
    def __init__(self, data: dict) -> None:
        self._data = data

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._data


class _FakeClient:
    def __init__(self, captured: dict, data: dict) -> None:
        self._captured = captured
        self._data = data

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *_: object) -> bool:
        return False

    async def post(self, url: str, json: dict, headers: dict) -> _FakeResponse:
        self._captured["json"] = json
        return _FakeResponse(self._data)


@pytest.mark.asyncio
async def test_invoke_model_sends_metadata_and_stamps_the_span(monkeypatch) -> None:
    from services.orchestration.temporal_activities import bedrock

    reset_for_testing()
    memory = InMemorySpanExporter()
    assert configure_tracing("gateway-test", exporter=memory) is True

    captured: dict = {}
    response_data = {
        "choices": [{"message": {"content": "ok"}}],
        "model": "bedrock/claude-x",
        "usage": {"prompt_tokens": 11, "completion_tokens": 4},
    }
    monkeypatch.setattr(
        bedrock.httpx,
        "AsyncClient",
        lambda **_: _FakeClient(captured, response_data),
    )

    result = await bedrock.invoke_model(
        bedrock.ModelRequest(
            logical_model="planning-model",
            prompt="hello",
            execution_id="exec-9",
            task_id="TASK-9",
            stage="plan",
            prompt_version="v2",
        )
    )
    assert result.model_id == "bedrock/claude-x"

    metadata = captured["json"]["metadata"]
    assert metadata["session_id"] == "exec-9"
    assert metadata["generation_name"] == "plan"
    assert metadata["version"] == "v2"

    force_flush()
    span = next(s for s in memory.get_finished_spans() if s.name == "llm.invoke")
    assert span.attributes[attrs.EXECUTION_ID] == "exec-9"
    assert span.attributes[attrs.STAGE] == "plan"
    assert span.attributes[attrs.GEN_AI_RESPONSE_MODEL] == "bedrock/claude-x"
    assert span.attributes[attrs.GEN_AI_USAGE_INPUT_TOKENS] == 11
    reset_for_testing()


@pytest.mark.asyncio
async def test_invoke_model_resolves_a_system_prompt(monkeypatch) -> None:
    from services.orchestration.temporal_activities import bedrock
    from services.prompts import StaticPromptStore, set_store

    set_store(StaticPromptStore())
    reset_for_testing()
    memory = InMemorySpanExporter()
    assert configure_tracing("gateway-system-prompt", exporter=memory) is True

    captured: dict = {}
    response_data = {
        "choices": [{"message": {"content": "ok"}}],
        "model": "bedrock/claude-x",
        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
    }
    monkeypatch.setattr(
        bedrock.httpx,
        "AsyncClient",
        lambda **_: _FakeClient(captured, response_data),
    )

    await bedrock.invoke_model(
        bedrock.ModelRequest(
            logical_model="planning-model",
            prompt="Task T-1.",
            execution_id="exec-p",
            task_id="TASK-p",
            stage="plan",
            system_prompt_name="planning",
        )
    )

    messages = captured["json"]["messages"]
    assert messages[0]["role"] == "system"
    assert "change plan" in messages[0]["content"]
    assert messages[1]["role"] == "user"
    assert captured["json"]["metadata"]["version"] == "planning/v1"

    force_flush()
    span = next(s for s in memory.get_finished_spans() if s.name == "llm.invoke")
    assert span.attributes[attrs.PROMPT_VERSION] == "planning/v1"
    set_store(None)
    reset_for_testing()
