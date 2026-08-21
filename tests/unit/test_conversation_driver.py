"""RemoteConversationDriver model routing for the LiteLLM proxy."""

from __future__ import annotations

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
