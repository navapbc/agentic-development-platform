import asyncio
from pathlib import Path

from openhands.runtime.caps import COMPLETED, RuntimeCaps
from openhands.runtime.conversation import FakeConversationDriver, RemoteConversationDriver
from openhands.runtime.provisioner import FakeSandboxProvisioner
from openhands.runtime.runtime import SandboxRuntime
from services.orchestration.materialization import MaterializedWorkspace
from services.orchestration.openhands_code_step import OpenHandsCodeStep


def _workspace(root: Path) -> MaterializedWorkspace:
    path = root / "agentic-demo-service" / "ADSER-1" / "exec-1"
    path.mkdir(parents=True)
    return MaterializedWorkspace(
        repo="agentic-demo-service",
        task_id="ADSER-1",
        execution_id="exec-1",
        branch="agent/ADSER-1/exec-1",
        base="main",
        commit_sha="abc123",
        path=str(path),
        branch_orphan_id="b1",
        workspace_orphan_id="w1",
    )


def _runtime(provisioner: FakeSandboxProvisioner, driver: FakeConversationDriver) -> SandboxRuntime:
    return SandboxRuntime(
        provisioner=provisioner,
        driver=driver,
        caps=RuntimeCaps(max_tool_calls=5, max_wall_clock_seconds=60.0),
        image="agent-server:test",
        runtime_class="",
        namespace="agent-sandbox",
        server_port=8000,
        litellm_base_url="http://host.docker.internal:4000",
    )


def test_code_step_maps_outcome_and_binds_workspace(tmp_path: Path) -> None:
    provisioner = FakeSandboxProvisioner()
    driver = FakeConversationDriver(tool_calls=2, cost=0.42, tokens=321, llm_calls=3)
    step = OpenHandsCodeStep(runtime=_runtime(provisioner, driver), workspace_root=tmp_path)
    workspace = _workspace(tmp_path)

    result = asyncio.run(step.run(workspace=workspace, task="Add a retry to the client"))

    assert result.terminal_reason == COMPLETED
    assert result.cost == 0.42
    assert result.tokens == 321
    assert result.llm_calls == 3

    spec = provisioner.specs[0]
    assert spec.workspace is not None
    assert spec.workspace.mount_path == "/workspace"
    expected = str((tmp_path / "agentic-demo-service" / "ADSER-1" / "exec-1").resolve())
    assert provisioner.copied_in[0][1] == expected
    assert provisioner.copied_in[0][2] == "/workspace/project"
    assert provisioner.torn_down


def test_code_step_applies_the_extracted_patch_to_the_workspace(tmp_path: Path) -> None:
    import subprocess

    patch = (
        "diff --git a/file.txt b/file.txt\n"
        "new file mode 100644\n"
        "index 0000000..3b18e51\n"
        "--- /dev/null\n"
        "+++ b/file.txt\n"
        "@@ -0,0 +1 @@\n"
        "+hello world\n"
    )
    provisioner = FakeSandboxProvisioner(patch=patch)
    driver = FakeConversationDriver(summary="done")
    step = OpenHandsCodeStep(runtime=_runtime(provisioner, driver), workspace_root=tmp_path)
    workspace = _workspace(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=workspace.path, check=True)

    asyncio.run(step.run(workspace=workspace, task="Create file.txt"))

    assert (Path(workspace.path) / "file.txt").read_text() == "hello world\n"
    assert provisioner.patched[0][1] == "/workspace/project"


def test_each_sandbox_gets_its_own_session_key_and_the_driver_presents_it(tmp_path: Path) -> None:
    provisioner = FakeSandboxProvisioner()
    driver = FakeConversationDriver()

    for attempt in ("first", "second"):
        step = OpenHandsCodeStep(
            runtime=_runtime(provisioner, driver), workspace_root=tmp_path / attempt
        )
        asyncio.run(step.run(workspace=_workspace(tmp_path / attempt), task=attempt))

    keys = [spec.session_api_key for spec in provisioner.specs]
    assert all(len(key) >= 32 for key in keys)
    assert keys[0] != keys[1]
    assert driver.session_api_keys == keys


def test_the_conversation_presents_the_key_issued_for_the_run(tmp_path: Path) -> None:
    provisioner = FakeSandboxProvisioner()
    unkeyed = FakeConversationDriver(fail=True)
    keyed = FakeConversationDriver(tool_calls=1)
    issued: list[str] = []

    async def driver_for_run(execution_id: str) -> FakeConversationDriver:
        issued.append(execution_id)
        return keyed

    step = OpenHandsCodeStep(
        runtime=_runtime(provisioner, unkeyed),
        workspace_root=tmp_path,
        driver_for_run=driver_for_run,
    )

    result = asyncio.run(step.run(workspace=_workspace(tmp_path), task="Add a retry"))

    assert result.terminal_reason == COMPLETED
    assert issued == ["exec-1"]
    assert unkeyed.calls == []
    assert len(keyed.calls) == 1


def test_the_built_code_step_never_hands_the_master_key_to_the_sandbox(monkeypatch) -> None:
    from services.integration import litellm_admin
    from services.orchestration.openhands_code_step import build_openhands_code_step
    from tests.support import hermetic_settings

    settings = hermetic_settings(litellm_master_key="sk-master-secret")

    async def ensure_run_model_key(execution_id, *, settings=None, client=None, grants=None):
        return f"sk-run-{execution_id}"

    monkeypatch.setattr(litellm_admin, "ensure_run_model_key", ensure_run_model_key)
    step = build_openhands_code_step(settings=settings)
    driver_for_run = step.driver_for_run
    assert driver_for_run is not None

    async def resolve():
        return await driver_for_run("exec-1")

    driver = asyncio.run(resolve())
    unkeyed = step.runtime.driver

    assert isinstance(driver, RemoteConversationDriver)
    assert isinstance(unkeyed, RemoteConversationDriver)
    assert driver._api_key == "sk-run-exec-1"
    assert unkeyed._api_key == ""
