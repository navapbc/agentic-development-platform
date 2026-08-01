"""RemoteConversationDriver model routing for the LiteLLM proxy."""

from __future__ import annotations

from openhands.runtime.conversation import RemoteConversationDriver


def test_bare_model_gets_litellm_proxy_prefix() -> None:
    driver = RemoteConversationDriver(model="coding-model", litellm_base_url="http://litellm:4000")
    assert driver._model == "litellm_proxy/coding-model"


def test_qualified_model_is_left_untouched() -> None:
    driver = RemoteConversationDriver(
        model="bedrock/us.anthropic.claude", litellm_base_url="http://litellm:4000"
    )
    assert driver._model == "bedrock/us.anthropic.claude"
