"""The scanners run from their own published image when subprocess execution is not available."""

from __future__ import annotations

import json

import pytest
from adapters.interfaces.security_scanner import ScanTarget
from adapters.real.security_scanner import (
    RealSemgrepScanner,
    ScannerError,
    run_subprocess,
)
from services.orchestration.gates.scanner_pod import (
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
