"""Hermetic tests for the platform prompt library and Langfuse-backed loader."""

from __future__ import annotations

import pytest
from services.orchestration.temporal_activities.implementation import (
    IMPLEMENTATION_PROMPT_VERSION,
)
from services.orchestration.temporal_activities.review import (
    _PROMPT_VERSIONS_BY_GATE,
    review_prompt_versions,
)
from services.prompts import (
    LangfusePromptStore,
    LoadedPrompt,
    StaticPromptStore,
    load_prompt,
    seed_prompts,
    set_store,
)
from services.prompts.library import REVIEW_PROMPTS


@pytest.fixture(autouse=True)
def _reset_store():
    set_store(None)
    yield
    set_store(None)


def test_library_versions_are_the_recorded_join_keys() -> None:
    assert REVIEW_PROMPTS["plan-review"].version == "plan-review/v1"
    assert REVIEW_PROMPTS["code-review"].version == "code-review/v1"
    assert REVIEW_PROMPTS["security-review"].version == "security-review/v1"
    assert REVIEW_PROMPTS["acceptance-verification"].version == "acceptance-verification/v1"
    assert IMPLEMENTATION_PROMPT_VERSION == "implementation/v1"


def test_coding_agent_directive_is_a_managed_prompt() -> None:
    set_store(StaticPromptStore())
    loaded = load_prompt("coding-agent")
    assert loaded.version == "coding-agent/v1"
    assert loaded.source == "static"
    assert "assigned task" in loaded.text


def test_planning_and_ci_repair_are_managed_prompts() -> None:
    set_store(StaticPromptStore())
    planning = load_prompt("planning")
    assert planning.version == "planning/v1"
    assert "change plan" in planning.text
    ci_repair = load_prompt("ci-repair")
    assert ci_repair.version == "ci-repair/v1"
    assert ci_repair.text.startswith("You are a CI repair planner")


def test_review_gate_version_map_reconciles_to_the_library() -> None:
    assert _PROMPT_VERSIONS_BY_GATE == {
        "plan-review": "plan-review/v1",
        "code-review": "code-review/v1",
        "security-review": "security-review/v1",
        "acceptance": "acceptance-verification/v1",
    }
    assert review_prompt_versions(["plan-review", "code-review"]) == {
        "plan-review": "plan-review/v1",
        "code-review": "code-review/v1",
    }


def test_static_store_returns_the_library_text() -> None:
    set_store(StaticPromptStore())
    loaded = load_prompt("plan-review")
    assert loaded.source == "static"
    assert loaded.version == "plan-review/v1"
    assert loaded.text.startswith("You are a rigorous software change-plan reviewer")


class _FakePrompt:
    def __init__(self, text: str, is_fallback: bool = False) -> None:
        self.prompt = text
        self.is_fallback = is_fallback


class _FakeClient:
    def __init__(self, text: str) -> None:
        self._text = text
        self.calls: list[tuple[str, str]] = []
        self.created: list[dict] = []

    def get_prompt(self, name, *, label=None, fallback=None, **_):
        self.calls.append((name, label))
        return _FakePrompt(self._text)

    def create_prompt(self, *, name, prompt, labels, type, commit_message):
        self.created.append({"name": name, "labels": labels})


class _RaisingClient:
    def get_prompt(self, *_, **__):
        raise RuntimeError("langfuse unreachable")


def test_langfuse_store_reads_text_and_keeps_the_version() -> None:
    client = _FakeClient("MANAGED plan prompt")
    set_store(LangfusePromptStore(client=client))
    loaded = load_prompt("plan-review")
    assert loaded.source == "langfuse"
    assert loaded.text == "MANAGED plan prompt"
    assert loaded.version == "plan-review/v1"
    assert client.calls == [("plan-review", "v1")]


def test_langfuse_store_falls_back_to_static_on_error() -> None:
    set_store(LangfusePromptStore(client=_RaisingClient()))
    loaded = load_prompt("code-review")
    assert isinstance(loaded, LoadedPrompt)
    assert loaded.source == "static"
    assert loaded.version == "code-review/v1"
    assert loaded.text.startswith("You are a rigorous code reviewer")


def test_seed_prompts_publishes_every_definition_with_its_label() -> None:
    client = _FakeClient("unused")
    seeded = seed_prompts(client=client)
    assert set(seeded) == {
        "plan-review/v1",
        "code-review/v1",
        "security-review/v1",
        "acceptance-verification/v1",
        "coding-agent/v1",
        "planning/v1",
        "ci-repair/v1",
    }
    labels = {entry["name"]: entry["labels"] for entry in client.created}
    assert labels["plan-review"] == ["v1", "production"]
    assert labels["coding-agent"] == ["v1", "production"]
