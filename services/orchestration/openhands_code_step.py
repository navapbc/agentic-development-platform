"""Pipeline code step that authors the bounded change with the OpenHands agent."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from openhands.runtime.conversation import RemoteConversationDriver
from openhands.runtime.provisioner import (
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

    async def run(self, *, workspace: MaterializedWorkspace, task: str) -> CodeResult:
        outcome = await self.runtime.run_implementation(
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
    resolved = settings or get_settings()
    llm_url = litellm_base_url or _DEFAULT_LITELLM_URL
    driver = RemoteConversationDriver(
        model=resolved.coding_model,
        litellm_base_url=llm_url,
        api_key=resolved.litellm_master_key,
    )
    provisioner = PortForwardSandboxProvisioner(
        KubernetesSandboxProvisioner(in_cluster=in_cluster, ready_timeout_seconds=600.0)
    )
    runtime = SandboxRuntime.from_settings(provisioner, driver, resolved)
    runtime.litellm_base_url = llm_url
    root = Path(workspace_root or resolved.execution_workspace_root)
    return OpenHandsCodeStep(runtime=runtime, workspace_root=root, context=context)
