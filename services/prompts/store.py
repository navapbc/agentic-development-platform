"""Prompt loading with a Langfuse-managed store and an in-code static fallback.

A stage resolves its prompt by name at build time. When Langfuse is reachable the
text is read from Langfuse by label, otherwise the in-code definition is used. The
version string is always the library value, so the determinism-manifest join key
is unchanged regardless of the source.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Protocol

from services.prompts.library import PROMPTS

log = logging.getLogger("prompts")


@dataclass(frozen=True)
class LoadedPrompt:
    name: str
    version: str
    text: str
    source: str


class PromptStore(Protocol):
    def get(self, name: str) -> LoadedPrompt: ...


class StaticPromptStore:
    def get(self, name: str) -> LoadedPrompt:
        definition = PROMPTS[name]
        return LoadedPrompt(definition.name, definition.version, definition.text, "static")


class LangfusePromptStore:
    def __init__(self, client: Any | None = None) -> None:
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            from langfuse import Langfuse

            self._client = Langfuse()
        return self._client

    def get(self, name: str) -> LoadedPrompt:
        definition = PROMPTS[name]
        try:
            client = self._get_client()
            prompt = client.get_prompt(name, label=definition.label, fallback=definition.text)
            text = prompt.prompt if isinstance(prompt.prompt, str) else definition.text
            source = "static" if getattr(prompt, "is_fallback", False) else "langfuse"
            return LoadedPrompt(definition.name, definition.version, text, source)
        except Exception:  # noqa: BLE001 - loading must not raise into a stage
            log.warning("langfuse prompt load failed for %s; using static", name, exc_info=True)
            return LoadedPrompt(definition.name, definition.version, definition.text, "static")


_store: PromptStore | None = None


def _default_store() -> PromptStore:
    if os.environ.get("LANGFUSE_PUBLIC_KEY"):
        return LangfusePromptStore()
    return StaticPromptStore()


def get_store() -> PromptStore:
    global _store
    if _store is None:
        _store = _default_store()
    return _store


def set_store(store: PromptStore | None) -> None:
    global _store
    _store = store


def load_prompt(name: str) -> LoadedPrompt:
    return get_store().get(name)


def seed_prompts(client: Any | None = None) -> list[str]:
    """Publish the library definitions to Langfuse. Returns the seeded versions."""
    if client is None:
        from langfuse import Langfuse

        client = Langfuse()
    seeded: list[str] = []
    for definition in PROMPTS.values():
        client.create_prompt(
            name=definition.name,
            prompt=definition.text,
            labels=[definition.label, "production"],
            type="text",
            commit_message=f"seed {definition.version}",
        )
        seeded.append(definition.version)
    return seeded
