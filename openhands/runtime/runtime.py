"""Compose sandbox provisioning and the conversation driver for one implementation stage."""

from __future__ import annotations

from dataclasses import dataclass, replace

from openhands.runtime.caps import RuntimeCaps
from openhands.runtime.conversation import ConversationDriver, ImplementationOutcome
from openhands.runtime.provisioner import SandboxProvisioner, SandboxSpec, WorkspaceBinding
from scripts.config import Settings, get_settings


@dataclass
class SandboxRuntime:
    provisioner: SandboxProvisioner
    driver: ConversationDriver
    caps: RuntimeCaps
    image: str
    runtime_class: str
    namespace: str
    server_port: int
    litellm_base_url: str
    workspace_mount_path: str = "/workspace"

    @classmethod
    def from_settings(
        cls,
        provisioner: SandboxProvisioner,
        driver: ConversationDriver,
        settings: Settings | None = None,
    ) -> SandboxRuntime:
        resolved = settings or get_settings()
        caps = RuntimeCaps(
            max_tool_calls=resolved.openhands_max_tool_calls,
            max_wall_clock_seconds=resolved.openhands_max_wall_clock_seconds,
        )
        return cls(
            provisioner=provisioner,
            driver=driver,
            caps=caps,
            image=resolved.openhands_agent_server_image,
            runtime_class=resolved.openhands_runtime_class,
            namespace=resolved.openhands_sandbox_namespace,
            server_port=resolved.openhands_server_port,
            litellm_base_url=resolved.litellm_base_url,
            workspace_mount_path=resolved.sandbox_workspace_mount_path,
        )

    async def run_implementation(
        self,
        *,
        execution_id: str,
        task: str,
        context: str = "",
        workspace_source: str = "",
        task_id: str = "",
    ) -> ImplementationOutcome:
        binding = (
            WorkspaceBinding(mount_path=self.workspace_mount_path) if workspace_source else None
        )
        spec = SandboxSpec(
            execution_id=execution_id,
            image=self.image,
            runtime_class=self.runtime_class,
            namespace=self.namespace,
            server_port=self.server_port,
            litellm_base_url=self.litellm_base_url,
            workspace=binding,
        )
        metadata = {"execution_id": execution_id, "stage": "implementing"}
        if task_id:
            metadata["ticket"] = task_id
        project_dir = f"{self.workspace_mount_path.rstrip('/')}/project" if binding else ""
        handle = await self.provisioner.provision(spec)
        try:
            if binding is not None:
                await self.provisioner.copy_in(handle, workspace_source, project_dir)
            outcome = await self.driver.run_task(
                base_url=handle.base_url,
                task=task,
                context=context,
                caps=self.caps,
                working_dir=project_dir,
                metadata=metadata,
            )
            if binding is not None:
                patch = await self.provisioner.extract_patch(handle, project_dir)
                if patch:
                    _apply_patch(workspace_source, patch)
                outcome = replace(outcome, patch=patch)
            return outcome
        finally:
            await self.provisioner.teardown(handle)


def _apply_patch(workspace_path: str, patch: str) -> None:
    import subprocess

    result = subprocess.run(
        ["git", "apply", "--whitespace=nowarn", "-"],
        cwd=workspace_path,
        input=patch.encode(),
        capture_output=True,
    )
    if result.returncode:
        raise RuntimeError(f"failed to apply agent patch: {result.stderr.decode().strip()}")
