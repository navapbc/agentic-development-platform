"""The scanners run from their own published image when subprocess execution is not available."""

from __future__ import annotations

import json

import pytest
from adapters.interfaces.security_scanner import ScannerUnavailableError, ScanTarget
from adapters.real.security_scanner import (
    RealSemgrepScanner,
    ScannerError,
    run_subprocess,
)
from services.orchestration.gates.pod_runner import PodEngineError
from services.orchestration.gates.scanner_pod import (
    IMAGE_PULL_EXIT,
    PodCommandRunner,
    remap_paths,
    restore_paths,
    scan_script,
    scanner_pod_name,
)

pytestmark = pytest.mark.asyncio


class FakePodOps:
    def __init__(self, exit_code: int = 0, stdout: str = "{}") -> None:
        self.created: list[dict] = []
        self.copied_in: list[tuple[str, str]] = []
        self.deleted: list[str] = []
        self.script = ""
        self._exit_code = exit_code
        self._stdout = stdout

    async def create(self, manifest):
        self.created.append(manifest)

    async def wait_ready(self, pod, namespace):
        return None

    async def copy_in(self, pod, namespace, source, dest):
        self.copied_in.append((source, dest))

    async def copy_out(self, pod, namespace, source, dest, manifest_path):
        return None

    async def exec_capture(self, pod, namespace, script):
        self.script = script
        return self._exit_code, self._stdout, ""

    async def delete(self, pod, namespace):
        self.deleted.append(pod)


async def test_subprocess_runner_names_the_missing_binary_and_the_pod_alternative():
    with pytest.raises(ScannerError) as excinfo:
        await run_subprocess(["definitely-not-installed", "scan"], "")

    message = str(excinfo.value)
    assert "definitely-not-installed is not installed" in message
    assert "scanner_execution" in message


def test_scanner_arguments_point_at_the_workspace_mount_not_the_worker_path():
    args = remap_paths(
        ["semgrep", "scan", "--json", "/home/appuser/checkout"], "/home/appuser/checkout"
    )

    assert args == ["semgrep", "scan", "--json", "/workspace"]


@pytest.mark.parametrize(
    ("arg", "expected"),
    [
        ("/home/appuser/checkout/", "/workspace"),
        ("/home/appuser/checkout/services/api.py", "/workspace/services/api.py"),
        ("/home/appuser/checkout-other", "/home/appuser/checkout-other"),
        ("--config=auto", "--config=auto"),
    ],
)
def test_scanner_arguments_below_the_checkout_are_remapped_too(arg, expected):
    assert remap_paths(["semgrep", arg], "/home/appuser/checkout") == ["semgrep", expected]


def test_restore_paths_leaves_a_json_array_document_unchanged():
    assert restore_paths('["/workspace/a.py"]', "/checkout") == '["/workspace/a.py"]'


def test_scan_script_isolates_the_tool_document_from_build_output():
    script = scan_script("semgrep/semgrep:1.177.0", ["semgrep", "scan", "--json", "/workspace"])

    assert "buildah from semgrep/semgrep:1.177.0" in script
    assert "> /tmp/.scanner-out.json" in script
    assert script.index("cat /tmp/.scanner-out.json") > script.index("buildah run")
    assert script.rstrip().endswith("exit $rc")


def test_scanner_pod_name_is_sanitised_and_bounded():
    name = scanner_pod_name("semgrep", "Exec/ID_With Junk" * 8)

    assert name.startswith("scanner-semgrep-")
    assert len(name) <= 63
    assert name == name.lower()


async def test_pod_runner_mounts_the_checkout_and_always_deletes_the_pod():
    ops = FakePodOps(stdout=json.dumps({"results": []}))
    runner = PodCommandRunner("docker.io/semgrep/semgrep:1.177.0", scanner="semgrep", ops=ops)

    result = await runner(["semgrep", "scan", "/checkout"], "/checkout")

    assert result.returncode == 0
    assert ops.copied_in == [("/checkout", "/workspace")]
    assert ops.deleted == [ops.created[0]["metadata"]["name"]]
    assert "/workspace" in ops.script


async def test_concurrent_scans_of_the_same_scanner_get_separate_pods():
    first, second = FakePodOps(), FakePodOps()
    await PodCommandRunner("img", scanner="semgrep", ops=first)(["semgrep"], "/a")
    await PodCommandRunner("img", scanner="semgrep", ops=second)(["semgrep"], "/b")

    assert first.deleted != second.deleted


async def test_pod_runner_feeds_the_adapter_so_findings_parse_unchanged():
    payload = {
        "results": [
            {
                "path": "app/main.py",
                "start": {"line": 12},
                "check_id": "python.lang.security.audit",
                "extra": {"severity": "ERROR", "message": "unsafe call"},
            }
        ]
    }
    ops = FakePodOps(stdout=json.dumps(payload))
    scanner = RealSemgrepScanner(
        runner=PodCommandRunner("semgrep/semgrep:1.177.0", scanner="semgrep", ops=ops)
    )

    findings = await scanner.scan(ScanTarget(repo="org/app", ref="abc123", repo_root="/checkout"))

    assert [f.rule_id for f in findings] == ["python.lang.security.audit"]
    assert findings[0].location == "app/main.py:12"


async def test_pod_runner_reports_findings_against_the_worker_path_not_the_mount():
    payload = {"results": [{"path": "/workspace/app/main.py", "start": {"line": 3},
                            "check_id": "rule", "extra": {"severity": "ERROR", "message": "m"}}]}
    ops = FakePodOps(stdout=json.dumps(payload))
    scanner = RealSemgrepScanner(
        runner=PodCommandRunner("docker.io/semgrep/semgrep:1.177.0", scanner="semgrep", ops=ops)
    )

    findings = await scanner.scan(
        ScanTarget(repo="org/app", ref="abc123", repo_root="/home/appuser/checkout")
    )

    assert findings[0].location == "/home/appuser/checkout/app/main.py:3"


def test_restore_paths_is_a_noop_without_a_checkout():
    assert restore_paths('{"path": "/workspace/a.py"}', "") == '{"path": "/workspace/a.py"}'


async def test_pod_runner_deletes_the_pod_when_the_scan_fails():
    ops = FakePodOps(exit_code=2, stdout="")
    runner = PodCommandRunner("docker.io/aquasec/trivy:0.74.0", scanner="trivy", ops=ops)

    result = await runner(["trivy", "fs", "/checkout"], "/checkout")

    assert result.returncode == 2
    assert ops.deleted == [ops.created[0]["metadata"]["name"]]


def test_restore_paths_rewrites_locations_without_touching_scanned_content():
    payload = json.dumps(
        {
            "results": [
                {
                    "path": "/workspace/app/main.py",
                    "extra": {"lines": 'MOUNT = "/workspace"'},
                }
            ]
        }
    )

    restored = json.loads(restore_paths(payload, "/checkout"))

    assert restored["results"][0]["path"] == "/checkout/app/main.py"
    assert restored["results"][0]["extra"]["lines"] == 'MOUNT = "/workspace"'


def test_restore_paths_returns_non_json_output_unchanged():
    assert restore_paths("not json /workspace", "/checkout") == "not json /workspace"


def test_scanner_output_file_sits_outside_the_scanned_workspace():
    script = scan_script("docker.io/semgrep/semgrep:1.177.0", ["semgrep", "scan", "/workspace"])

    assert "/workspace/.scanner-out.json" not in script
    assert "/tmp/.scanner-out.json" in script


def test_restore_paths_rewrites_scanned_and_error_locations():
    payload = json.dumps(
        {
            "results": [{"path": "/workspace/a.py"}],
            "errors": [{"path": "/workspace/b.py", "message": "at /workspace/b.py"}],
            "paths": {"scanned": ["/workspace/a.py"], "skipped": [{"path": "/workspace/c.py"}]},
        }
    )

    restored = json.loads(restore_paths(payload, "/checkout"))

    assert restored["results"][0]["path"] == "/checkout/a.py"
    assert restored["errors"][0]["path"] == "/checkout/b.py"
    assert restored["paths"]["scanned"] == ["/checkout/a.py"]
    assert restored["paths"]["skipped"][0]["path"] == "/checkout/c.py"
    assert restored["errors"][0]["message"] == "at /workspace/b.py"


class _FailingPodOps(FakePodOps):
    """A pod transport that fails at one named lifecycle step."""

    def __init__(self, failing_step: str) -> None:
        super().__init__()
        self._failing_step = failing_step

    async def create(self, manifest):
        if self._failing_step == "create":
            raise PodEngineError("failed to pull image docker.io/semgrep/semgrep")
        await super().create(manifest)

    async def wait_ready(self, pod, namespace):
        if self._failing_step == "wait_ready":
            raise PodEngineError("pod never became ready")
        return None

    async def exec_capture(self, pod, namespace, script):
        if self._failing_step == "exec_capture":
            raise PodEngineError("kubectl exec lost the connection")
        return await super().exec_capture(pod, namespace, script)


@pytest.mark.parametrize("failing_step", ["create", "wait_ready", "exec_capture"])
async def test_pod_transport_failure_is_unavailability_not_a_finding(failing_step):
    """The mediator retries unavailability but turns any other exception into a finding."""
    ops = _FailingPodOps(failing_step)
    runner = PodCommandRunner("docker.io/semgrep/semgrep:1.2.3", scanner="semgrep", ops=ops)

    with pytest.raises(ScannerUnavailableError):
        await runner(["semgrep", "scan", "/checkout"], "/checkout")


async def test_a_created_pod_is_deleted_even_when_the_scan_fails():
    ops = _FailingPodOps("exec_capture")
    runner = PodCommandRunner("docker.io/semgrep/semgrep:1.2.3", scanner="semgrep", ops=ops)

    with pytest.raises(ScannerUnavailableError):
        await runner(["semgrep", "scan", "/checkout"], "/checkout")

    assert ops.deleted, "the pod must be torn down when the scan fails"


def test_scan_script_exits_a_reserved_code_when_the_scanner_image_cannot_be_pulled():
    """A failed pull must be distinguishable from the scanner's own exit codes."""
    script = scan_script("semgrep/semgrep:1.177.0", ["semgrep", "scan", "--json", "/workspace"])

    assert f"|| exit {IMAGE_PULL_EXIT}" in script
    assert script.index("|| exit") < script.index("buildah run")
    assert IMAGE_PULL_EXIT not in range(0, 14), "semgrep defines 0-13 and trivy emits 0-2"


async def test_a_scanner_image_that_cannot_be_pulled_is_unavailability_not_a_finding():
    """The pull runs inside the pod, so it returns an exit code rather than a PodEngineError."""
    ops = FakePodOps(exit_code=IMAGE_PULL_EXIT, stdout="")
    runner = PodCommandRunner("docker.io/semgrep/semgrep:1.2.3", scanner="semgrep", ops=ops)

    with pytest.raises(ScannerUnavailableError) as raised:
        await runner(["semgrep", "scan", "/checkout"], "/checkout")

    assert "docker.io/semgrep/semgrep:1.2.3" in str(raised.value)
    assert ops.deleted, "the pod must be torn down when the pull fails"


@pytest.mark.parametrize("exit_code", [0, 1, 2])
async def test_the_scanner_reporting_through_its_own_exit_code_is_not_unavailability(exit_code):
    """Only the reserved code means infrastructure. The tool's own codes reach the adapter."""
    ops = FakePodOps(exit_code=exit_code, stdout="{}")
    runner = PodCommandRunner("docker.io/semgrep/semgrep:1.2.3", scanner="semgrep", ops=ops)

    result = await runner(["semgrep", "scan", "/checkout"], "/checkout")

    assert result.returncode == exit_code


class _Settings:
    """The subset of settings the scanner backend selection reads."""

    def __init__(self, mode: str) -> None:
        self.scanner_execution = mode
        self.semgrep_image = "docker.io/semgrep/semgrep:1.177.0"
        self.trivy_image = "docker.io/aquasec/trivy:0.74.0"
        self.gate_engine_namespace = "gates"
        self.gate_engine_builder_image = "quay.io/buildah/stable:v1.38.0"
        self.gate_engine_privileged = True
        self.semgrep_binary = "semgrep"
        self.trivy_binary = "trivy"


@pytest.mark.parametrize("scanner", ["semgrep", "trivy"])
def test_subprocess_is_the_default_backend_and_carries_no_pod_runner(scanner):
    from adapters.real.security_scanner import RealSemgrepScanner, RealTrivyScanner
    from services.orchestration.gates.builtin import _scanner_backend

    factory = {"semgrep": RealSemgrepScanner, "trivy": RealTrivyScanner}[scanner]
    built = _scanner_backend(scanner, factory.from_settings, _Settings("subprocess"))

    assert not isinstance(built._runner, PodCommandRunner)


@pytest.mark.parametrize(
    ("scanner", "image"),
    [("semgrep", "docker.io/semgrep/semgrep:1.177.0"), ("trivy", "docker.io/aquasec/trivy:0.74.0")],
)
def test_pod_mode_wires_the_scanner_to_its_own_pinned_image(scanner, image):
    """This is the whole switch, so a mis-wire would silently keep shelling out on the worker."""
    from adapters.real.security_scanner import RealSemgrepScanner, RealTrivyScanner
    from services.orchestration.gates.builtin import _scanner_backend

    factory = {"semgrep": RealSemgrepScanner, "trivy": RealTrivyScanner}[scanner]
    built = _scanner_backend(scanner, factory.from_settings, _Settings("pod"))

    assert isinstance(built._runner, PodCommandRunner)
    assert built._runner._image == image
    assert built._runner._scanner == scanner
    assert built._runner._privileged is True


def test_an_unrecognised_scanner_execution_mode_is_rejected_rather_than_silently_subprocess():
    from services.integration.credentials import ConfigError
    from services.orchestration.gates.builtin import _scanner_backend

    with pytest.raises(ConfigError) as raised:
        _scanner_backend("semgrep", lambda *a, **k: None, _Settings("POD"))

    assert "scanner_execution" in str(raised.value)


def test_a_trivy_report_comes_back_against_the_worker_path_not_the_mount():
    """Trivy locates findings under Results/Target, which no semgrep-shaped test exercises."""
    document = json.dumps(
        {
            "Results": [
                {"Target": "/workspace/package-lock.json", "Vulnerabilities": []},
                {"Target": "/workspace", "Vulnerabilities": []},
            ]
        }
    )

    restored = json.loads(restore_paths(document, "/checkout"))

    assert [r["Target"] for r in restored["Results"]] == [
        "/checkout/package-lock.json",
        "/checkout",
    ]


async def test_a_trivy_scan_through_the_pod_parses_into_findings():
    from adapters.interfaces.security_scanner import ScanTarget
    from adapters.real.security_scanner import RealTrivyScanner

    document = json.dumps(
        {
            "Results": [
                {
                    "Target": "/workspace/package-lock.json",
                    "Vulnerabilities": [
                        {
                            "VulnerabilityID": "CVE-2024-0001",
                            "Severity": "HIGH",
                            "Title": "a vulnerable dependency",
                            "PkgName": "left-pad",
                        }
                    ],
                }
            ]
        }
    )
    ops = FakePodOps(exit_code=0, stdout=document)
    runner = PodCommandRunner("docker.io/aquasec/trivy:0.74.0", scanner="trivy", ops=ops)
    scanner = RealTrivyScanner(runner=runner)

    findings = await scanner.scan(
        ScanTarget(repo="org/app", ref="HEAD", repo_root="/checkout")
    )

    assert [f.severity for f in findings] == ["high"]
    assert findings[0].location.startswith("/checkout/package-lock.json")
    assert "/workspace" not in findings[0].location
    assert "trivy fs" in ops.script


async def test_a_scanner_that_exits_clean_without_a_document_is_unavailable_not_a_clean_scan():
    """An empty document would otherwise parse to zero findings and pass the gate."""
    ops = FakePodOps(exit_code=0, stdout="   \n")
    runner = PodCommandRunner("docker.io/semgrep/semgrep:1.2.3", scanner="semgrep", ops=ops)

    with pytest.raises(ScannerUnavailableError) as raised:
        await runner(["semgrep", "scan", "/checkout"], "/checkout")

    assert "no output" in str(raised.value)
    assert ops.deleted, "the pod must be torn down"
