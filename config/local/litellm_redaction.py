"""LiteLLM logging-path callback that redacts secrets before generations reach Langfuse.

Runs only on the logging hook, so the model still receives the original prompt while
the text sent to Langfuse has credential-shaped substrings replaced. The module is
mounted into the LiteLLM container and referenced from the proxy config as
``litellm_redaction.redactor``. It imports nothing from the platform repository so it
can load inside the LiteLLM image.
"""

from __future__ import annotations

import re
from typing import Any

from litellm.integrations.custom_logger import CustomLogger

_PLACEHOLDER = "[REDACTED]"

_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ASIA[0-9A-Z]{16}"),
    re.compile(r"ghp_[0-9A-Za-z]{36}"),
    re.compile(r"gho_[0-9A-Za-z]{36}"),
    re.compile(r"github_pat_[0-9A-Za-z_]{22,}"),
    re.compile(r"xox[baprs]-[0-9A-Za-z-]{10,}"),
    re.compile(r"sk-[0-9A-Za-z]{20,}"),
    re.compile(r"-----BEGIN[^-]*PRIVATE KEY-----.*?-----END[^-]*PRIVATE KEY-----", re.DOTALL),
    re.compile(r"(?i)\b(?:bearer|token|secret|password|passwd|api[_-]?key)\b\s*[:=]\s*\S+"),
    re.compile(r"(?i)aws_secret_access_key\s*[:=]\s*\S+"),
)


def _redact_text(text: str) -> str:
    redacted = text
    for pattern in _PATTERNS:
        redacted = pattern.sub(_PLACEHOLDER, redacted)
    return redacted


def _redact_content(content: Any) -> Any:
    if isinstance(content, str):
        return _redact_text(content)
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append({**part, "text": _redact_text(part["text"])})
            else:
                parts.append(part)
        return parts
    return content


def _redact_messages(messages: Any) -> Any:
    if not isinstance(messages, list):
        return messages
    redacted = []
    for message in messages:
        if isinstance(message, dict) and "content" in message:
            redacted.append({**message, "content": _redact_content(message["content"])})
        else:
            redacted.append(message)
    return redacted


def _redact_result(result: Any) -> Any:
    choices = getattr(result, "choices", None)
    if not choices:
        return result
    for choice in choices:
        message = getattr(choice, "message", None)
        text = getattr(message, "content", None)
        if isinstance(text, str):
            message.content = _redact_text(text)
    return result


class _Redactor(CustomLogger):
    def logging_hook(
        self, kwargs: dict, result: Any, call_type: str
    ) -> tuple[dict, Any]:
        if isinstance(kwargs.get("messages"), list):
            kwargs["messages"] = _redact_messages(kwargs["messages"])
        return kwargs, _redact_result(result)

    async def async_logging_hook(
        self, kwargs: dict, result: Any, call_type: str
    ) -> tuple[dict, Any]:
        return self.logging_hook(kwargs, result, call_type)


redactor = _Redactor()
