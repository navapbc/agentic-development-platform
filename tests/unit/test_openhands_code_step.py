import asyncio
from pathlib import Path

from openhands.runtime.caps import COMPLETED, RuntimeCaps
from openhands.runtime.conversation import FakeConversationDriver
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
