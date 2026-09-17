"""Sandbox copy-in and patch extraction build git commands that survive agent commits."""

from __future__ import annotations

import shutil
import subprocess

from openhands.runtime.provisioner import (
    _BASE_TAG,
    KubernetesSandboxProvisioner,
    SandboxHandle,
)


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

    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    provisioner = KubernetesSandboxProvisioner(in_cluster=False)

    provisioner._copy_in_sync(_handle(), "/host/src", "/workspace/project")

    cmd = captured["copy_cmd"]
    assert "git add -A" in cmd
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

    monkeypatch.setattr(subprocess, "run", _fake_run)
    provisioner = KubernetesSandboxProvisioner(in_cluster=False)

    patch = provisioner._extract_patch_sync(_handle(), "/workspace/project")

    cmd = captured["extract_cmd"]
    assert "git add -A" in cmd
    assert f"git diff --cached {_BASE_TAG}" in cmd
    assert "git diff --cached)" in cmd
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


async def test_provision_retries_after_a_terminating_pod_conflict(monkeypatch):
    from openhands.runtime import provisioner as provisioner_module
    from openhands.runtime.provisioner import SandboxSpec

    core = _FakeCore(create_conflicts=1, present_reads=2)
    provisioner = _fast_provisioner(core)

    async def _ready(self, core, name, namespace):
        return "10.0.0.5"

    monkeypatch.setattr(provisioner_module.KubernetesSandboxProvisioner, "_wait_ready", _ready)
    spec = SandboxSpec(execution_id="exec-9", image="img:latest")

    handle = await provisioner.provision(spec)

    assert core.created == 1
    assert core.create_conflicts == 0
    assert handle.base_url == "http://10.0.0.5:8000"
