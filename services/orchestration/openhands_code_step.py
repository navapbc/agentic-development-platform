"""Pipeline code step that authors the bounded change with the OpenHands agent."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path

from openhands.runtime.conversation import ConversationDriver, RemoteConversationDriver
from openhands.runtime.provisioner import (
    SANDBOX_READY_TIMEOUT_SECONDS,
    KubernetesSandboxProvisioner,
    PortForwardSandboxProvisioner,
)
from openhands.runtime.runtime import SandboxRuntime
from scripts.config import Settings, get_settings

from services.orchestration.bounded_task_pipeline import CodeResult
from services.orchestration.materialization import MaterializedWorkspace

_DEFAULT_LITELLM_URL = "http://host.docker.internal:4000"


@dataclass
class OpenHandsCodeStep:
    runtime: SandboxRuntime
    workspace_root: Path
    context: str = ""
    driver_for_run: Callable[[str], Awaitable[ConversationDriver]] | None = None

    async def run(self, *, workspace: MaterializedWorkspace, task: str) -> CodeResult:
        runtime = self.runtime
        if self.driver_for_run is not None:
            runtime = replace(runtime, driver=await self.driver_for_run(workspace.execution_id))
        outcome = await runtime.run_implementation(
            execution_id=workspace.execution_id,
            task=task,
            context=self.context,
            workspace_source=str(Path(workspace.path).resolve()),
            task_id=workspace.task_id,
        )
        return CodeResult(
            cost=outcome.cost,
            tokens=outcome.tokens,
            llm_calls=outcome.llm_calls,
            terminal_reason=outcome.terminal_reason,
            summary=outcome.summary,
        )


def build_openhands_code_step(
    *,
    settings: Settings | None = None,
    litellm_base_url: str | None = None,
    workspace_root: str | Path | None = None,
    context: str = "",
    in_cluster: bool = False,
) -> OpenHandsCodeStep:
    from services.integration.litellm_admin import ensure_run_model_key

    resolved = settings or get_settings()
    llm_url = litellm_base_url or _DEFAULT_LITELLM_URL

    def driver_with_key(api_key: str) -> RemoteConversationDriver:
        return RemoteConversationDriver(
            model=resolved.coding_model, litellm_base_url=llm_url, api_key=api_key
        )

    async def driver_for_run(execution_id: str) -> ConversationDriver:
        from services.orchestration.limit_grants import PostgresLimitGrants

        key = await ensure_run_model_key(
            execution_id, settings=resolved, grants=PostgresLimitGrants()
        )
        return driver_with_key(key)

    provisioner = PortForwardSandboxProvisioner(
        KubernetesSandboxProvisioner(
            in_cluster=in_cluster, ready_timeout_seconds=SANDBOX_READY_TIMEOUT_SECONDS
        )
    )
    runtime = SandboxRuntime.from_settings(provisioner, driver_with_key(""), resolved)
    runtime.litellm_base_url = llm_url
    root = Path(workspace_root or resolved.execution_workspace_root)
    return OpenHandsCodeStep(
        runtime=runtime, workspace_root=root, context=context, driver_for_run=driver_for_run
    )
