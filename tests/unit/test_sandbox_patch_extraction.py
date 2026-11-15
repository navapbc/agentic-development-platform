"""Sandbox copy-in and patch extraction build git commands that survive agent commits."""

from __future__ import annotations

import shutil
import subprocess

from openhands.runtime.provisioner import (
    _BASE_TAG,
    KubernetesSandboxProvisioner,
    SandboxHandle,
    SandboxSpec,
    build_pod_manifest,
)
from services import process


def _handle() -> SandboxHandle:
    return SandboxHandle(name="pod-1", namespace="agent-sandbox", base_url="http://pod-1:8000")


def test_copy_in_configures_identity_and_tags_the_base(monkeypatch):
    captured: dict[str, str] = {}

    class _FakePopen:
        def __init__(self, args, **kwargs):
            if args and args[0] == "tar":
                self.stdout = None
            else:
                captured["copy_cmd"] = args[-1]
                self.stdout = None
            self.returncode = 0

        def communicate(self):
            return b"", b""

        def wait(self):
            return 0

    monkeypatch.setattr(process, "resolve_executable", lambda name, path=None: name)

    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    provisioner = KubernetesSandboxProvisioner(in_cluster=False)

    provisioner._copy_in_sync(_handle(), "/host/src", "/workspace/project")

    cmd = captured["copy_cmd"]
    assert 'git config user.email "openhands@all-hands.dev"' in cmd
    assert 'git config user.name "openhands"' in cmd
    assert f"git tag -f {_BASE_TAG} HEAD" in cmd


def test_copy_in_commits_the_copied_tree_before_tagging_the_base(monkeypatch):
    captured: dict[str, str] = {}

    class _FakePopen:
        def __init__(self, args, **kwargs):
            if args and args[0] == "tar":
                self.stdout = None
            else:
                captured["copy_cmd"] = args[-1]
                self.stdout = None
            self.returncode = 0

        def communicate(self):
            return b"", b""

        def wait(self):
            return 0

    monkeypatch.setattr(process, "resolve_executable", lambda name, path=None: name)

    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    provisioner = KubernetesSandboxProvisioner(in_cluster=False)

    provisioner._copy_in_sync(_handle(), "/host/src", "/workspace/project")

    cmd = captured["copy_cmd"]
    assert "add -A" in cmd
    assert "commit --no-verify" in cmd
    assert cmd.index("git add -A") < cmd.index("commit --no-verify")
    assert cmd.index("commit --no-verify") < cmd.index(f"git tag -f {_BASE_TAG} HEAD")


def test_copy_in_tolerates_a_workspace_that_was_copied_in_clean(monkeypatch):
    captured: dict[str, str] = {}

    class _FakePopen:
        def __init__(self, args, **kwargs):
            if args and args[0] == "tar":
                self.stdout = None
            else:
                captured["copy_cmd"] = args[-1]
                self.stdout = None
            self.returncode = 0

        def communicate(self):
            return b"", b""

        def wait(self):
            return 0

    monkeypatch.setattr(process, "resolve_executable", lambda name, path=None: name)

    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    provisioner = KubernetesSandboxProvisioner(in_cluster=False)

    provisioner._copy_in_sync(_handle(), "/host/src", "/workspace/project")

    assert "--allow-empty" in captured["copy_cmd"]


def test_copy_in_tags_the_base_despite_project_signing_config_and_hooks(tmp_path, monkeypatch):
    source = tmp_path / "src"
    source.mkdir()

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=source, check=True, capture_output=True)

    git("init", "-q", "-b", "main", ".")
    git("config", "user.email", "seed@example.com")
    git("config", "user.name", "seed")
    (source / "a.txt").write_text("a\n")
    git("add", "-A")
    git("commit", "-qm", "seed")
    git("config", "commit.gpgsign", "true")
    hook = source / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    (source / "b.txt").write_text("b\n")

    captured: dict[str, str] = {}

    class _CapturePopen:
        def __init__(self, args, **kwargs):
            if args and args[0] != "tar":
                captured["copy_cmd"] = args[-1]
            self.stdout = None
            self.returncode = 0

        def communicate(self):
            return b"", b""

        def wait(self):
            return 0

    monkeypatch.setattr(process, "resolve_executable", lambda name, path=None: name)

    monkeypatch.setattr(subprocess, "Popen", _CapturePopen)
    KubernetesSandboxProvisioner(in_cluster=False)._copy_in_sync(
        _handle(), str(source), "/workspace/project"
    )
    monkeypatch.undo()

    dest = tmp_path / "dest"
    shutil.copytree(source, dest)
    git_chain = captured["copy_cmd"].split("cd /workspace/project && ", 1)[1]
    result = subprocess.run(["sh", "-c", git_chain], cwd=dest, capture_output=True)

    assert result.returncode == 0, result.stderr.decode()
    tagged = subprocess.run(
        ["git", "show", "--stat", "--format=%s", _BASE_TAG],
        cwd=dest,
        capture_output=True,
        text=True,
    )
    assert tagged.returncode == 0
    assert "b.txt" in tagged.stdout


def test_extract_patch_diffs_against_the_base_tag_with_a_fallback(monkeypatch):
    captured: dict[str, str] = {}

    def _fake_run(args, **kwargs):
        captured["extract_cmd"] = args[-1]
        return subprocess.CompletedProcess(args, 0, stdout="diff", stderr="")

    monkeypatch.setattr(process, "resolve_executable", lambda name, path=None: name)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    provisioner = KubernetesSandboxProvisioner(in_cluster=False)

    patch = provisioner._extract_patch_sync(_handle(), "/workspace/project")

    cmd = captured["extract_cmd"]
    assert "add -A" in cmd
    assert f"git diff --cached --binary {_BASE_TAG}" in cmd
    assert "git diff --cached --binary)" in cmd
    assert patch == "diff"


class _FakeCore:
    def __init__(self, *, create_conflicts: int = 0, present_reads: int = 0):
        self.create_conflicts = create_conflicts
        self.present_reads = present_reads
        self.created = 0
        self.deleted: list = []

    def create_namespaced_pod(self, namespace, manifest):
        from kubernetes.client.exceptions import ApiException

        if self.create_conflicts > 0:
            self.create_conflicts -= 1
            raise ApiException(status=409)
        self.created += 1

    def read_namespaced_pod(self, name, namespace):
        from kubernetes.client.exceptions import ApiException

        if self.present_reads > 0:
            self.present_reads -= 1
            return object()
        raise ApiException(status=404)

    def delete_namespaced_pod(self, name, namespace, grace_period_seconds=None):
        self.deleted.append(grace_period_seconds)


def _fast_provisioner(core):
    p = KubernetesSandboxProvisioner(in_cluster=False, poll_interval_seconds=0.0)
    p._core = core
    return p


async def test_teardown_force_deletes_and_waits_until_absent():
    core = _FakeCore(present_reads=2)
    provisioner = _fast_provisioner(core)

    await provisioner.teardown(_handle())

    assert core.deleted == [0]
    assert core.present_reads == 0


async def test_provision_replaces_the_pod_an_earlier_attempt_left(monkeypatch):
    from openhands.runtime import provisioner as provisioner_module
    from openhands.runtime.provisioner import SandboxSpec

    core = _FakeCore(create_conflicts=1, present_reads=2)
    provisioner = _fast_provisioner(core)

    async def _ready(self, core, name, namespace):
        return "10.0.0.5"

    monkeypatch.setattr(provisioner_module.KubernetesSandboxProvisioner, "_wait_ready", _ready)
    spec = SandboxSpec(execution_id="exec-9", image="img:latest")

    handle = await provisioner.provision(spec)

    assert core.deleted == [0]
    assert core.created == 1
    assert core.create_conflicts == 0
    assert handle.base_url == "http://10.0.0.5:8000"


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _sandbox_pair(tmp_path, tracked: dict[str, bytes]):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "-q")
    for name, body in tracked.items():
        (workspace / name).parent.mkdir(parents=True, exist_ok=True)
        (workspace / name).write_bytes(body)
    _git(workspace, "add", "-A")
    _git(workspace, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-qm", "x")
    sandbox = tmp_path / "sandbox"
    shutil.copytree(workspace, sandbox)
    _git(sandbox, "tag", _BASE_TAG)
    return workspace, sandbox


def _extract_from(sandbox, monkeypatch) -> str:
    real_run = subprocess.run

    def _run_in_sandbox(args, **kwargs):
        script = args[-1].replace("/workspace/project", str(sandbox))
        kwargs.pop("executable", None)
        return real_run(["sh", "-c", script], **kwargs)

    with monkeypatch.context() as patched:
        patched.setattr(process, "resolve_executable", lambda name, path=None: name)
        patched.setattr(subprocess, "run", _run_in_sandbox)
        return KubernetesSandboxProvisioner(in_cluster=False)._extract_patch_sync(
            _handle(), "/workspace/project"
        )


def test_an_extracted_patch_carrying_a_binary_file_applies_to_the_workspace(tmp_path, monkeypatch):
    from openhands.runtime.runtime import _apply_patch

    workspace, sandbox = _sandbox_pair(tmp_path, {"calc.py": b"def add(a, b):\n    return a + b\n"})
    (sandbox / "logo.png").write_bytes(bytes(range(256)))
    (sandbox / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
    )

    _apply_patch(str(workspace), _extract_from(sandbox, monkeypatch))

    assert (workspace / "logo.png").read_bytes() == bytes(range(256))
    assert "def mul" in (workspace / "calc.py").read_text()


def test_the_extracted_patch_leaves_out_untracked_build_artifacts(tmp_path, monkeypatch):
    workspace, sandbox = _sandbox_pair(
        tmp_path, {"calc.py": b"x = 1\n", "build/generate.py": b"y = 1\n"}
    )
    (sandbox / "calc.py").write_text("x = 2\n")
    (sandbox / "build" / "generate.py").write_text("y = 2\n")
    for artifact in (
        "__pycache__/calc.cpython-313.pyc",
        "tests/__pycache__/test_calc.cpython-313.pyc",
        "stray.pyc",
        ".pytest_cache/v/cache/lastfailed",
        "node_modules/left-pad/index.js",
        "target/classes/App.class",
        "build/output.bin",
        "dist/calc-0.1.tar.gz",
        ".venv/bin/python",
    ):
        (sandbox / artifact).parent.mkdir(parents=True, exist_ok=True)
        (sandbox / artifact).write_bytes(b"\x00artifact")

    patch = _extract_from(sandbox, monkeypatch)

    changed = sorted(
        line.split(" b/")[-1] for line in patch.splitlines() if line.startswith("diff --git")
    )
    assert changed == ["build/generate.py", "calc.py"]


def test_the_sandbox_writes_no_python_bytecode() -> None:
    env = build_pod_manifest(SandboxSpec(execution_id="exec-1", image="img@sha256:" + "a" * 64))[
        "spec"
    ]["containers"][0]["env"]

    assert {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"} in env
