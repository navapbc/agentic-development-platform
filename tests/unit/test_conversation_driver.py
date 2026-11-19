"""RemoteConversationDriver model routing for the LiteLLM proxy."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from openhands.runtime.caps import RuntimeCaps
from openhands.runtime.conversation import RemoteConversationDriver, _coding_agent_directive
from services.prompts import StaticPromptStore, set_store


def test_bare_model_gets_litellm_proxy_prefix() -> None:
    driver = RemoteConversationDriver(model="coding-model", litellm_base_url="http://litellm:4000")
    assert driver._model == "litellm_proxy/coding-model"


def test_qualified_model_is_left_untouched() -> None:
    driver = RemoteConversationDriver(
        model="bedrock/us.anthropic.claude", litellm_base_url="http://litellm:4000"
    )
    assert driver._model == "bedrock/us.anthropic.claude"


def test_coding_agent_directive_loads_the_managed_prompt() -> None:
    set_store(StaticPromptStore())
    try:
        directive = _coding_agent_directive()
    finally:
        set_store(None)
    assert "assigned task" in directive

    from openhands.sdk import AgentContext

    context = AgentContext(system_message_suffix=directive)
    assert context.system_message_suffix == directive


@pytest.fixture
def remote_conversation(monkeypatch):
    events = []
    observed = {}

    class Conversation:
        def __init__(self, **kwargs):
            self.callbacks = kwargs["callbacks"]
            self.state = SimpleNamespace(events=[])
            metrics = SimpleNamespace(
                accumulated_token_usage=SimpleNamespace(
                    prompt_tokens=2,
                    completion_tokens=3,
                    cache_read_tokens=0,
                    cache_write_tokens=0,
                ),
                accumulated_cost=0.125,
                costs=[0.125],
            )
            self.conversation_stats = SimpleNamespace(get_combined_metrics=lambda: metrics)

        def send_message(self, message):
            observed["task"] = message

        def run(self):
            if "error" in observed:
                raise RuntimeError(observed["error"])
            self.state.events.extend(events)
            for event in events[: observed.get("callback_count", len(events))]:
                for callback in self.callbacks:
                    callback(event)

        def close(self):
            observed["closed"] = True

    import openhands.sdk
    import openhands.tools.preset.default

    monkeypatch.setattr(openhands.sdk, "LLM", lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr(openhands.sdk, "Workspace", lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr(openhands.sdk, "Conversation", Conversation)
    monkeypatch.setattr(
        openhands.tools.preset.default, "get_default_agent", lambda **kwargs: SimpleNamespace()
    )
    monkeypatch.setattr("openhands.runtime.conversation._coding_agent_directive", lambda: "")

    def run():
        driver = RemoteConversationDriver(
            model="coding-model", litellm_base_url="http://litellm.invalid"
        )
        return driver._run_sync(
            "http://sandbox.invalid", "Task", "Context", RuntimeCaps(max_wall_clock_seconds=60)
        )

    return events, observed, run


def _message(source, role, *texts):
    from openhands.sdk.event import MessageEvent
    from openhands.sdk.llm import Message, TextContent

    return MessageEvent(
        source=source,
        llm_message=Message(role=role, content=[TextContent(text=text) for text in texts]),
    )


def test_remote_driver_returns_final_agent_message_as_summary(remote_conversation):
    events, observed, run = remote_conversation
    events.extend(
        [
            _message("user", "user", "Task details"),
            _message("agent", "assistant", "Work in progress"),
            _message("agent", "assistant", "Completed.\n", "Checks passed."),
        ]
    )

    result = run()

    assert result.summary == "Completed.\nChecks passed."
    assert result.status == "finished"
    assert result.terminal_reason == "completed"
    assert result.tokens == 5
    assert result.cost == 0.125
    assert observed == {"task": "Task\n\n---\n\nContext", "closed": True}


def test_remote_driver_does_not_replace_agent_summary_with_user_message(remote_conversation):
    events, _, run = remote_conversation
    events.extend(
        [
            _message("agent", "assistant", "Agent response"),
            _message("user", "user", "Later user message"),
        ]
    )

    assert run().summary == "Agent response"


def test_remote_driver_without_agent_message_keeps_summary_empty(remote_conversation):
    events, observed, run = remote_conversation
    events.append(_message("user", "user", "Task details"))

    assert run().summary == ""
    assert observed["closed"]


def test_remote_driver_preserves_error_summary_and_closes_conversation(remote_conversation):
    _, observed, run = remote_conversation
    observed["error"] = "Conversation failed"

    result = run()

    assert result.summary == "Conversation failed"
    assert result.status == "failed"
    assert result.terminal_reason == "error"
    assert observed["closed"]


@pytest.mark.parametrize("callback_count", [0, 1])
def test_remote_driver_reads_the_final_reconciled_message(remote_conversation, callback_count):
    events, observed, run = remote_conversation
    events.extend(
        [
            _message("agent", "assistant", "Earlier response"),
            _message("agent", "assistant", "Final response recovered from the server"),
        ]
    )
    observed["callback_count"] = callback_count

    assert run().summary == "Final response recovered from the server"
    assert observed["closed"]


def test_remote_driver_excludes_reasoning_and_prompt_extensions(remote_conversation):
    from openhands.sdk.event import MessageEvent
    from openhands.sdk.llm import Message, TextContent

    events, _, run = remote_conversation
    events.append(
        MessageEvent(
            source="agent",
            llm_message=Message(
                role="assistant",
                content=[TextContent(text="Completion report")],
                reasoning_content="Reasoning",
            ),
            extended_content=[TextContent(text="Prompt extension")],
        )
    )

    assert run().summary == "Completion report"
