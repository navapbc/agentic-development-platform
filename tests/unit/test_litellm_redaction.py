"""Hermetic tests for the LiteLLM logging-path secret redaction callback."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

_MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "local" / "litellm_redaction.py"
)
_spec = importlib.util.spec_from_file_location("litellm_redaction", _MODULE_PATH)
redaction = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(redaction)


def test_redacts_aws_and_github_and_bearer_tokens():
    text = (
        "use AKIAIOSFODNN7EXAMPLE and ghp_012345678901234567890123456789012345 "
        "and Authorization: Bearer abc.def.ghi and password=hunter2"
    )
    out = redaction._redact_text(text)
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert "ghp_012345678901234567890123456789012345" not in out
    assert "hunter2" not in out
    assert "[REDACTED]" in out


def test_keeps_ordinary_code_untouched():
    text = "def add(a, b):\n    return a + b\n"
    assert redaction._redact_text(text) == text


def test_redacts_private_key_block():
    text = "-----BEGIN RSA PRIVATE KEY-----\nabc\ndef\n-----END RSA PRIVATE KEY-----"
    assert "PRIVATE KEY" not in redaction._redact_text(text)


def test_redacts_message_list_content():
    messages = [
        {"role": "system", "content": "review this"},
        {"role": "user", "content": "token=xoxb-1234567890-abcdefghijkl"},
    ]
    out = redaction._redact_messages(messages)
    assert out[0]["content"] == "review this"
    assert "xoxb-1234567890" not in out[1]["content"]


def test_redacts_structured_content_parts():
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "key sk-abcdefghijklmnopqrstuvwx"}]}
    ]
    out = redaction._redact_messages(messages)
    assert "sk-abcdefghijklmnopqrstuvwx" not in out[0]["content"][0]["text"]


def test_logging_hook_redacts_messages_and_result():
    message = SimpleNamespace(content="leaked AKIAIOSFODNN7EXAMPLE here")
    result = SimpleNamespace(choices=[SimpleNamespace(message=message)])
    kwargs = {"messages": [{"role": "user", "content": "AKIAIOSFODNN7EXAMPLE"}]}

    new_kwargs, new_result = redaction.redactor.logging_hook(kwargs, result, "completion")
    assert "AKIAIOSFODNN7EXAMPLE" not in new_kwargs["messages"][0]["content"]
    assert "AKIAIOSFODNN7EXAMPLE" not in new_result.choices[0].message.content
