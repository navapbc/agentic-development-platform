import json

import pytest
from services.operator_remedy import NEXT_STEP_PREFIX
from services.orchestration.gates.pod_runner import (
    CHECKS_POD_RESOURCES,
    DEFAULT_POD_RESOURCES,
    PodEngineError,
    PodEngineRunner,
    PodEngineSpec,
    _run_script,
    _tail,
    build_pod_manifest,
    describe_pod_not_ready,
    pod_name_for,
)


class _FakePodOps:
    def __init__(self, *, exec_result=(0, "{}", ""), fail_ready=False):
        self._exec_result = exec_result
        self._fail_ready = fail_ready
        self.created = []
        self.copied = []
        self.execed = []
        self.deleted = []
        self.ready = []

    async def create(self, manifest):
        self.created.append(manifest)

    async def wait_ready(self, pod, namespace):
        if self._fail_ready:
            raise PodEngineError("not ready")
        self.ready.append((pod, namespace))

    async def copy_in(self, pod, namespace, source_dir, dest_dir):
        self.copied.append((source_dir, dest_dir))

    async def copy_out(self, pod, namespace, source_dir, dest_dir, manifest_path):
        return None

    async def exec_capture(self, pod, namespace, script):
        self.execed.append(script)
        return self._exec_result

    async def delete(self, pod, namespace):
        self.deleted.append((pod, namespace))


def _spec(**over):
    base = dict(
        name="ai-security-review",
        execution_id="exec-1",
        checkout_dir="/tmp/checkout",
        entrypoint="engines/security-compliance-review/harness/ai-pr-review",
        base_ref="abc123",
        source_path="/tmp/engine",
        env={"AI_REVIEW_TOOL": "claude", "AI_REVIEW_PROFILE": "cms-ars"},
    )
    base.update(over)
    return PodEngineSpec(**base)


async def test_run_engine_builds_copies_execs_and_parses():
    doc = {"review_action": "COMMENT", "comments": [{"severity": "HIGH"}]}
    ops = _FakePodOps(exec_result=(0, json.dumps(doc), ""))
    runner = PodEngineRunner(ops)

    result = await runner.run_engine(_spec())

    assert result == doc
    assert ops.copied == [("/tmp/engine", "/engine"), ("/tmp/checkout", "/workspace")]
    assert ops.deleted, "pod must be torn down"
    script = ops.execed[0]
    assert "buildah bud -t gate-tool /engine" in script
    assert "--against abc123" in script
    assert "-e AI_REVIEW_PROFILE=cms-ars" in script
    assert "-e AI_REVIEW_TOOL=claude" in script


async def test_image_mode_skips_the_build():
    ops = _FakePodOps(exec_result=(0, "{}", ""))
    runner = PodEngineRunner(ops)
    await runner.run_engine(_spec(source_path="", image="returntocorp/semgrep:1.0"))
    script = ops.execed[0]
    assert "buildah bud" not in script
    assert "img=returntocorp/semgrep:1.0" in script
    assert ops.copied == [("/tmp/checkout", "/workspace")]


async def test_nonzero_exit_raises_and_tears_down():
    ops = _FakePodOps(exec_result=(2, "", "boom"))
    runner = PodEngineRunner(ops)
    with pytest.raises(PodEngineError, match="exited 2"):
        await runner.run_engine(_spec())
    assert ops.deleted, "pod must be torn down even on failure"


async def test_unparseable_output_raises():
    ops = _FakePodOps(exec_result=(0, "not json", ""))
    runner = PodEngineRunner(ops)
    with pytest.raises(PodEngineError, match="no parseable JSON"):
        await runner.run_engine(_spec())


async def test_teardown_runs_when_ready_fails():
    ops = _FakePodOps(fail_ready=True)
    runner = PodEngineRunner(ops)
    with pytest.raises(PodEngineError):
        await runner.run_engine(_spec())
    assert ops.deleted


def test_run_script_treats_exit_1_as_success():
    script = _run_script(_spec())
    assert "[ $rc -ne 0 ] && [ $rc -ne 1 ]" in script


def test_run_script_makes_the_workspace_writable_for_a_nonroot_engine_user():
    script = _run_script(_spec())
    assert "chmod 0777 /workspace" in script


def test_run_script_commits_the_working_tree_snapshot_before_review():
    script = _run_script(_spec())
    # The agent's change is the uncommitted working tree, committed as root so the engine's
    # `git diff <base> HEAD` sees it. The commit must precede the review invocation.
    assert "git add -A" in script
    assert "commit -q -m gate-review-snapshot" in script
    assert "buildah run --user root" in script
    assert script.index("gate-review-snapshot") < script.index("--against")


def test_run_script_disables_git_ownership_check_for_the_mounted_checkout():
    script = _run_script(_spec())
    assert "GIT_CONFIG_KEY_0=safe.directory" in script
    assert "GIT_CONFIG_VALUE_0=*" in script


def test_run_script_repoints_the_host_gateway_alias_to_pod_dns():
    script = _run_script(_spec())
    assert "getent hosts host.docker.internal" in script
    assert "--add-host host.docker.internal:$gw" in script
    assert "buildah from $hostflag" in script


def test_run_script_disables_tls_verify_for_an_insecure_registry_pull():
    script = _run_script(
        _spec(source_path="", image="host.docker.internal:5001/gate-tool:519dd2f", tls_verify=False)
    )
    assert "buildah from --tls-verify=false $hostflag" in script


def test_run_script_keeps_tls_verify_on_by_default_in_image_mode():
    script = _run_script(_spec(source_path="", image="host.docker.internal:5001/gate-tool:519dd2f"))
    assert "--tls-verify=false" not in script
    assert "buildah from $hostflag" in script


def test_run_script_never_adds_tls_flag_in_build_mode():
    # Build mode uses a local tag with no registry pull, so tls_verify is irrelevant even if off.
    script = _run_script(_spec(tls_verify=False))
    assert "--tls-verify=false" not in script


def test_tail_returns_the_end_of_a_long_stream():
    text = "head-marker " + ("x" * 5000) + " tail-marker"
    tailed = _tail(text, limit=100)
    assert tailed.endswith("tail-marker")
    assert "head-marker" not in tailed
    assert tailed.startswith("...(truncated)...")


def test_tail_returns_short_text_unchanged():
    assert _tail("  brief error  ") == "brief error"


def test_pod_name_is_sanitised_and_bounded():
    name = pod_name_for("AI_Security/Review", "exec-1")
    assert name.startswith("gate-engine-")
    assert len(name) <= 63
    assert name == name.lower()


def _security_context(manifest):
    return manifest["spec"]["containers"][0]["securityContext"]


def test_pod_manifest_is_unprivileged_by_default():
    manifest = build_pod_manifest("pod-1", "agent-sandbox", "builder:1")
    assert _security_context(manifest) == {"runAsUser": 0}


def test_pod_manifest_grants_privilege_when_requested():
    manifest = build_pod_manifest("pod-1", "agent-sandbox", "builder:1", True)
    assert _security_context(manifest) == {"runAsUser": 0, "privileged": True}


def test_pod_manifest_defaults_to_the_small_gate_allocation():
    manifest = build_pod_manifest("pod-1", "agent-sandbox", "builder:1")
    assert manifest["spec"]["containers"][0]["resources"] == DEFAULT_POD_RESOURCES


def test_pod_manifest_accepts_a_larger_allocation():
    manifest = build_pod_manifest(
        "pod-1", "agent-sandbox", "builder:1", resources=CHECKS_POD_RESOURCES
    )
    assert manifest["spec"]["containers"][0]["resources"]["limits"]["cpu"] == "6"


def test_pod_manifest_does_not_share_mutable_resource_state():
    first = build_pod_manifest("pod-1", "agent-sandbox", "builder:1")
    first["spec"]["containers"][0]["resources"]["limits"]["cpu"] = "99"
    second = build_pod_manifest("pod-2", "agent-sandbox", "builder:1")
    assert second["spec"]["containers"][0]["resources"]["limits"]["cpu"] == "2"
    assert DEFAULT_POD_RESOURCES["limits"]["cpu"] == "2"


async def test_runner_propagates_privilege_to_the_manifest():
    ops = _FakePodOps()
    runner = PodEngineRunner(ops, privileged=True)
    await runner.run_engine(
        PodEngineSpec(
            name="gate",
            execution_id="exec-1",
            checkout_dir="/tmp/checkout",
            entrypoint="run.sh",
            base_ref="main",
            image="img",
        )
    )
    assert _security_context(ops.created[0])["privileged"] is True


class _FakeProc:
    def __init__(self, returncode=0, stderr=b""):
        self.returncode = returncode
        self.stdout = None
        self._stderr = stderr

    def communicate(self):
        return b"", self._stderr

    def wait(self):
        return self.returncode


def _patch_copy_out(monkeypatch, *, exec_rc=0, tar_rc=0, stderr=b""):
    """Stand in for the kubectl-exec-into-tar pipe so the wiring can be asserted without a pod."""
    import subprocess

    from services import process

    calls: list[list[str]] = []

    def fake_popen(argv, **kwargs):
        calls.append(argv)
        if argv[0] == "tar":
            return _FakeProc(tar_rc)
        return _FakeProc(exec_rc, stderr)

    monkeypatch.setattr(process, "resolve_executable", lambda name, path=None: name)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    return calls


@pytest.mark.asyncio(loop_scope="function")
async def test_copy_out_streams_the_manifest_through_tar_into_the_destination(monkeypatch):
    """copy_out is how a verified fix leaves the pod, so its wiring must be pinned."""
    from services.orchestration.gates.pod_runner import KubectlPodOps

    calls = _patch_copy_out(monkeypatch)
    await KubectlPodOps().copy_out("pod-1", "ns", "/workspace", "/staging", "/workspace/.list")

    exec_argv, tar_argv = calls
    assert exec_argv[:2] == ["kubectl", "exec"]
    assert "pod-1" in exec_argv and "ns" in exec_argv
    script = exec_argv[-1]
    assert "tar cf -" in script
    assert "--ignore-failed-read" in script
    assert "-C /workspace" in script
    assert "-T /workspace/.list" in script
    assert tar_argv == ["tar", "xmf", "-", "--no-same-owner", "-C", "/staging"]


@pytest.mark.parametrize(("exec_rc", "tar_rc"), [(1, 0), (0, 1), (1, 1)])
@pytest.mark.asyncio(loop_scope="function")
async def test_a_failed_copy_out_is_raised_rather_than_silently_staging_nothing(
    monkeypatch, exec_rc, tar_rc
):
    from services.orchestration.gates.pod_runner import KubectlPodOps

    _patch_copy_out(monkeypatch, exec_rc=exec_rc, tar_rc=tar_rc, stderr=b"tar: cannot read")

    with pytest.raises(PodEngineError):
        await KubectlPodOps().copy_out("pod-1", "ns", "/workspace", "/staging", "/workspace/.list")


def _unschedulable_status(message="0/1 nodes are available: 1 Insufficient cpu."):
    return {
        "phase": "Pending",
        "conditions": [
            {
                "type": "PodScheduled",
                "status": "False",
                "reason": "Unschedulable",
                "message": message,
            }
        ],
    }


def test_an_unschedulable_pod_is_described_as_a_resource_shortfall():
    text = describe_pod_not_ready(_unschedulable_status(), "p", "n")

    assert "Pending" in text
    assert "Unschedulable" in text
    assert "Insufficient cpu" in text
    assert f"{NEXT_STEP_PREFIX} Give the cluster node more CPU" in text
    assert "lower DEFAULT_POD_RESOURCES or CHECKS_POD_RESOURCES" in text


def test_a_pod_whose_image_will_not_pull_names_the_registry_as_the_next_check():
    text = describe_pod_not_ready(
        {
            "phase": "Pending",
            "conditions": [{"type": "PodScheduled", "status": "True"}],
            "containerStatuses": [
                {
                    "state": {
                        "waiting": {
                            "reason": "ImagePullBackOff",
                            "message": "back-off pulling image quay.io/buildah/stable",
                        }
                    }
                }
            ],
        },
        "p",
        "n",
    )

    assert "ImagePullBackOff" in text
    assert "quay.io/buildah/stable" in text
    assert text.startswith("The pod is Pending and its container is waiting")
    assert f"{NEXT_STEP_PREFIX} Check that GATE_ENGINE_BUILDER_IMAGE" in text


def test_a_pod_with_no_explaining_condition_still_reports_its_phase():
    text = describe_pod_not_ready({"phase": "Running"}, "runner-7", "gates")

    assert "Running" in text
    assert f"{NEXT_STEP_PREFIX} Run `kubectl describe pod -n gates runner-7`" in text


class _CompletedProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _patch_wait_ready(monkeypatch, *, status_payload, get_rc=0, get_stderr=""):
    """Stand in for `kubectl wait` timing out and the follow-up `kubectl get pod -o json`."""
    from services import process

    def fake_run(argv, **kwargs):
        if argv[1] == "wait":
            return _CompletedProc(1, "", "error: timed out waiting for the condition")
        assert argv[1:3] == ["get", "pod"]
        return _CompletedProc(get_rc, json.dumps(status_payload), get_stderr)

    monkeypatch.setattr(process, "run", fake_run)


@pytest.mark.asyncio(loop_scope="function")
async def test_a_readiness_timeout_reports_why_the_pod_never_scheduled(monkeypatch):
    """A `kubectl wait` timeout alone cannot distinguish a starved cluster from a slow one."""
    from services.orchestration.gates.pod_runner import KubectlPodOps

    _patch_wait_ready(monkeypatch, status_payload={"status": _unschedulable_status()})

    with pytest.raises(PodEngineError) as caught:
        await KubectlPodOps(ready_timeout_seconds=180.0).wait_ready("checks-1", "agent-sandbox")

    message = str(caught.value)
    assert "checks-1" in message
    assert "agent-sandbox" in message
    assert "180s" in message
    assert "Unschedulable" in message
    assert "Insufficient cpu" in message
    assert "timed out waiting for the condition" in message


@pytest.mark.asyncio(loop_scope="function")
async def test_a_readiness_timeout_survives_an_unreadable_pod_status(monkeypatch):
    from services.orchestration.gates.pod_runner import KubectlPodOps

    _patch_wait_ready(
        monkeypatch, status_payload={}, get_rc=1, get_stderr='pods "checks-1" not found'
    )

    with pytest.raises(PodEngineError) as caught:
        await KubectlPodOps(ready_timeout_seconds=30.0).wait_ready("checks-1", "agent-sandbox")

    assert 'pods "checks-1" not found' in str(caught.value)
