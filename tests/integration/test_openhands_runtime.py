"""Hermetic sandbox runtime: per-execution provisioning, teardown, and caps abort."""

from __future__ import annotations

from openhands.runtime.caps import RuntimeCaps
from openhands.runtime.conversation import FakeConversationDriver
from openhands.runtime.provisioner import FakeSandboxProvisioner
from openhands.runtime.runtime import SandboxRuntime

EXECUTION_ID = "AGENTDEV-1-exec-1"
TASK = "Implement the feature."


def _runtime(driver: FakeConversationDriver, caps: RuntimeCaps) -> SandboxRuntime:
    return SandboxRuntime(
        provisioner=FakeSandboxProvisioner(),
        driver=driver,
        caps=caps,
        image="ghcr.io/openhands/agent-server:latest-python",
        runtime_class="",
        namespace="agent-sandbox",
        server_port=8000,
        litellm_base_url="http://litellm:4000",
    )


async def test_provisions_and_tears_down_one_pod_per_execution() -> None:
    driver = FakeConversationDriver(tool_calls=3, summary="done")
    runtime = _runtime(driver, RuntimeCaps())

    outcome = await runtime.run_implementation(execution_id=EXECUTION_ID, task=TASK)

    assert outcome.status == "finished"
    assert outcome.terminal_reason == "completed"
    assert outcome.tool_calls == 3
    provisioner = runtime.provisioner
    assert isinstance(provisioner, FakeSandboxProvisioner)
    assert len(provisioner.provisioned) == 1
    assert provisioner.torn_down == provisioner.provisioned
    assert driver.calls == [provisioner.provisioned[0].base_url]


async def test_tool_call_cap_aborts_the_stage() -> None:
    driver = FakeConversationDriver(tool_calls=61)
    runtime = _runtime(driver, RuntimeCaps(max_tool_calls=60))

    outcome = await runtime.run_implementation(execution_id=EXECUTION_ID, task=TASK)

    assert outcome.status == "aborted"
    assert outcome.terminal_reason == "tool_calls_exceeded"
    assert outcome.tool_calls == 61
    provisioner = runtime.provisioner
    assert isinstance(provisioner, FakeSandboxProvisioner)
    assert len(provisioner.torn_down) == 1


async def test_pod_is_torn_down_when_the_driver_fails() -> None:
    driver = FakeConversationDriver(fail=True)
    runtime = _runtime(driver, RuntimeCaps())

    outcome = await runtime.run_implementation(execution_id=EXECUTION_ID, task=TASK)

    assert outcome.status == "failed"
    assert outcome.terminal_reason == "error"
    provisioner = runtime.provisioner
    assert isinstance(provisioner, FakeSandboxProvisioner)
    assert len(provisioner.torn_down) == 1
