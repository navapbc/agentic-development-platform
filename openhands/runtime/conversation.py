"""Conversation drivers that run an implementation stage against an agent-server."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from openhands.runtime.caps import (
    COMPLETED,
    ERROR,
    WALL_CLOCK_EXCEEDED,
    CapTracker,
    RuntimeCapExceeded,
    RuntimeCaps,
)

FINISHED = "finished"
ABORTED = "aborted"
FAILED = "failed"
_MAX_OUTPUT_TOKENS = 8192


@dataclass(frozen=True)
class ImplementationOutcome:
    status: str
    terminal_reason: str
    tool_calls: int
    elapsed_seconds: float
    cost: float = 0.0
    tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    llm_calls: int = 0
    summary: str = ""
    patch: str = ""


@runtime_checkable
class ConversationDriver(Protocol):
    async def run_task(
        self,
        *,
        base_url: str,
        task: str,
        context: str,
        caps: RuntimeCaps,
        working_dir: str = "",
        metadata: dict[str, str] | None = None,
    ) -> ImplementationOutcome: ...


class RemoteConversationDriver:
    def __init__(self, *, model: str, litellm_base_url: str, api_key: str = "") -> None:
        self._model = model if "/" in model else f"litellm_proxy/{model}"
        self._litellm_base_url = litellm_base_url
        self._api_key = api_key

    async def run_task(
        self,
        *,
        base_url: str,
        task: str,
        context: str,
        caps: RuntimeCaps,
        working_dir: str = "",
        metadata: dict[str, str] | None = None,
    ) -> ImplementationOutcome:
        return await asyncio.to_thread(
            self._run_sync, base_url, task, context, caps, working_dir, metadata
        )

    def _run_sync(
        self,
        base_url: str,
        task: str,
        context: str,
        caps: RuntimeCaps,
        working_dir: str = "",
        metadata: dict[str, str] | None = None,
    ) -> ImplementationOutcome:
        from openhands.sdk import LLM, AgentContext, Conversation, Workspace
        from openhands.sdk.event import ActionEvent
        from openhands.tools.preset.default import get_default_agent
        from pydantic import SecretStr

        tracker = CapTracker(caps)
        breach: dict[str, str] = {}
        extra_body = _coding_extra_body(metadata)

        llm = LLM(
            usage_id="coding",
            model=self._model,
            base_url=self._litellm_base_url,
            api_key=SecretStr(self._api_key or "unused"),
            max_output_tokens=_MAX_OUTPUT_TOKENS,
            caching_prompt=True,
            reasoning_effort="none",
            litellm_extra_body=extra_body,
        )
        agent = get_default_agent(llm=llm, cli_mode=True)
        suffix = _coding_agent_directive()
        if suffix:
            agent = agent.model_copy(
                update={"agent_context": AgentContext(system_message_suffix=suffix)}
            )
        workspace = Workspace(host=base_url, working_dir=working_dir or "workspace/project")

        def on_event(event: object) -> None:
            if isinstance(event, ActionEvent):
                try:
                    tracker.record_tool_call()
                except RuntimeCapExceeded as exc:
                    breach.setdefault("reason", exc.reason)
                    _stop(conversation)
            elif tracker.exceeded_wall_clock():
                breach.setdefault("reason", WALL_CLOCK_EXCEEDED)
                _stop(conversation)

        conversation = Conversation(agent=agent, workspace=workspace, callbacks=[on_event])
        watchdog = threading.Timer(
            caps.max_wall_clock_seconds,
            lambda: (breach.setdefault("reason", WALL_CLOCK_EXCEEDED), _stop(conversation)),
        )
        watchdog.daemon = True
        try:
            conversation.send_message(_compose(task, context))
            watchdog.start()
            conversation.run()
            reason = breach.get("reason", COMPLETED)
            status = ABORTED if reason != COMPLETED else FINISHED
            metrics = conversation.conversation_stats.get_combined_metrics()
            usage = metrics.accumulated_token_usage
            prompt_tokens = int(usage.prompt_tokens) if usage else 0
            completion_tokens = int(usage.completion_tokens) if usage else 0
            cache_read_tokens = int(usage.cache_read_tokens) if usage else 0
            cache_write_tokens = int(usage.cache_write_tokens) if usage else 0
            return ImplementationOutcome(
                status=status,
                terminal_reason=reason,
                tool_calls=tracker.tool_calls,
                elapsed_seconds=tracker.elapsed,
                cost=float(metrics.accumulated_cost),
                tokens=prompt_tokens + completion_tokens,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
                llm_calls=len(metrics.costs),
            )
        except Exception as exc:
            return ImplementationOutcome(
                status=FAILED,
                terminal_reason=ERROR,
                tool_calls=tracker.tool_calls,
                elapsed_seconds=tracker.elapsed,
                summary=str(exc),
            )
        finally:
            watchdog.cancel()
            conversation.close()


def _stop(conversation: object) -> None:
    pause = getattr(conversation, "pause", None)
    if callable(pause):
        pause()


def _compose(task: str, context: str) -> str:
    if not context:
        return task
    return f"{task}\n\n---\n\n{context}"


def _coding_agent_directive() -> str:
    """Load the platform coding-agent directive from Langfuse. Never raises."""
    try:
        from services.prompts import load_prompt

        return load_prompt("coding-agent").text
    except Exception:  # noqa: BLE001 - a prompt load must not break the coding step
        return ""


def _coding_agent_version() -> str:
    """Return the coding-agent prompt version. Never raises."""
    try:
        from services.prompts.library import PROMPTS

        return PROMPTS["coding-agent"].version
    except Exception:  # noqa: BLE001 - a version lookup must not break the coding step
        return ""


def _coding_extra_body(metadata: dict[str, str] | None) -> dict[str, object]:
    """Build the LiteLLM request body that joins the coding generation to the session.

    The Bedrock ``requestMetadata`` carries the AWS-side attribution. The LiteLLM
    ``metadata`` carries the Langfuse join keys, so the coding generation groups
    under the execution session with its stage and prompt version.
    """
    if not metadata:
        return {}
    extra_body: dict[str, object] = {"requestMetadata": dict(metadata)}
    join: dict[str, Any] = {}
    if metadata.get("execution_id"):
        join["session_id"] = metadata["execution_id"]
    if metadata.get("stage"):
        join["generation_name"] = metadata["stage"]
    version = _coding_agent_version()
    if version:
        join["version"] = version
    if metadata.get("execution_id") or metadata.get("ticket"):
        join["trace_metadata"] = {
            key: metadata[key] for key in ("execution_id", "ticket") if metadata.get(key)
        }
    if join:
        extra_body["metadata"] = join
    return extra_body


class FakeConversationDriver:
    def __init__(
        self,
        *,
        tool_calls: int = 1,
        cost: float = 0.0,
        tokens: int = 0,
        llm_calls: int = 0,
        summary: str = "",
        fail: bool = False,
    ) -> None:
        self._tool_calls = tool_calls
        self._cost = cost
        self._tokens = tokens
        self._llm_calls = llm_calls
        self._summary = summary
        self._fail = fail
        self.calls: list[str] = []
        self.metadata: dict[str, str] | None = None

    async def run_task(
        self,
        *,
        base_url: str,
        task: str,
        context: str,
        caps: RuntimeCaps,
        working_dir: str = "",
        metadata: dict[str, str] | None = None,
    ) -> ImplementationOutcome:
        self.calls.append(base_url)
        self.metadata = metadata
        tracker = CapTracker(caps)
        if self._fail:
            return ImplementationOutcome(
                status=FAILED,
                terminal_reason=ERROR,
                tool_calls=0,
                elapsed_seconds=tracker.elapsed,
                summary="driver failure",
            )
        try:
            for _ in range(self._tool_calls):
                tracker.record_tool_call()
        except RuntimeCapExceeded as exc:
            return ImplementationOutcome(
                status=ABORTED,
                terminal_reason=exc.reason,
                tool_calls=tracker.tool_calls,
                elapsed_seconds=tracker.elapsed,
            )
        return ImplementationOutcome(
            status=FINISHED,
            terminal_reason=COMPLETED,
            tool_calls=tracker.tool_calls,
            elapsed_seconds=tracker.elapsed,
            cost=self._cost,
            tokens=self._tokens,
            llm_calls=self._llm_calls,
            summary=self._summary,
        )
