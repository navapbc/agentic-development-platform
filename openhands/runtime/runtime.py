"""Compose sandbox provisioning and the conversation driver for one implementation stage."""

from __future__ import annotations

from dataclasses import dataclass

from openhands.runtime.caps import RuntimeCaps
from openhands.runtime.conversation import ConversationDriver, ImplementationOutcome
from openhands.runtime.provisioner import SandboxProvisioner, SandboxSpec
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
        )

    async def run_implementation(
        self,
        *,
        execution_id: str,
        task: str,
        context: str = "",
    ) -> ImplementationOutcome:
        spec = SandboxSpec(
            execution_id=execution_id,
            image=self.image,
            runtime_class=self.runtime_class,
            namespace=self.namespace,
            server_port=self.server_port,
            litellm_base_url=self.litellm_base_url,
        )
        handle = await self.provisioner.provision(spec)
        try:
            return await self.driver.run_task(
                base_url=handle.base_url,
                task=task,
                context=context,
                caps=self.caps,
            )
        finally:
            await self.provisioner.teardown(handle)
