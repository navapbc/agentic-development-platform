"""A gate, checks, or scanner pod pulls a private ECR image with a token the worker copies in."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from services.orchestration.checks.runner import ChecksSpec, _checks_script, _fix_script
from services.orchestration.gates import registry_auth
from services.orchestration.gates.pod_runner import PodEngineSpec, _run_script
from services.orchestration.gates.scanner_pod import PodCommandRunner, scan_script

pytestmark = pytest.mark.usefixtures("allow_any_image")

ECR = "123456789012.dkr.ecr.us-east-1.amazonaws.com"
TRIVY = f"{ECR}/agentic-platform/trivy:0.74.0@sha256:{'a' * 64}"
TOKEN = "dG9rZW4="
PULLING = registry_auth.RegistrySetup(authenticated=True)


class _CopyingOps:
    """Record what each copy_in would deliver, reading the source before it is removed."""

    def __init__(self) -> None:
        self.copied: dict[str, dict[str, tuple[int, str]]] = {}
        self.script = ""

    async def create(self, manifest):
        self.manifest = manifest

    async def wait_ready(self, pod, namespace):
        return None

    async def copy_in(self, pod, namespace, source_dir, dest_dir):
        self.copied[dest_dir] = {
            path.name: (stat.S_IMODE(path.stat().st_mode), path.read_text())
            for path in Path(source_dir).iterdir()
        }

    async def exec_capture(self, pod, namespace, script):
        self.script = script
        return 0, json.dumps({"Results": []}), ""

    async def delete(self, pod, namespace):
        return None


def test_only_ecr_hosts_need_a_token() -> None:
    registries = registry_auth.ecr_registries(
        [
            TRIVY,
            "999999999999.dkr.ecr-fips.us-gov-west-1.amazonaws.com/x:1",
            "docker.io/aquasec/trivy:0.74.0",
            "localhost:5001/gate:1",
            "",
            "busybox",
        ]
    )

    assert registries == {
        ECR: ("123456789012", "us-east-1"),
        "999999999999.dkr.ecr-fips.us-gov-west-1.amazonaws.com": ("999999999999", "us-gov-west-1"),
    }


async def test_install_copies_an_owner_only_auth_file_for_each_ecr_registry() -> None:
    ops = _CopyingOps()
    requested: list[tuple[str, str]] = []

    def token(account: str, region: str) -> str:
        requested.append((account, region))
        return TOKEN

    installed = await registry_auth.install(
        ops, "pod", "agent-sandbox", [TRIVY, TRIVY], token, registries=None
    )

    assert installed.authenticated
    assert requested == [("123456789012", "us-east-1")]
    mode, content = ops.copied[registry_auth.AUTH_DIR]["auth.json"]
    assert mode == 0o600
    assert json.loads(content) == {"auths": {ECR: {"auth": TOKEN}}}


async def test_install_does_nothing_for_an_image_outside_ecr() -> None:
    ops = _CopyingOps()

    def token(account: str, region: str) -> str:
        raise AssertionError("no token is requested")

    setup = await registry_auth.install(
        ops, "pod", "ns", ["docker.io/semgrep/semgrep:1"], token, registries=None
    )

    assert setup == registry_auth.NO_REGISTRY
    assert set(ops.copied) == {"/etc/containers"}


def _pull_window(script: str) -> tuple[int, int, int]:
    return (
        script.index("export REGISTRY_AUTH_FILE="),
        script.index("buildah from"),
        script.index(f"rm -rf {registry_auth.AUTH_DIR}"),
    )


def test_each_script_reads_the_auth_file_only_for_the_pull() -> None:
    spec = PodEngineSpec(
        name="gate",
        execution_id="e",
        checkout_dir="/c",
        entrypoint="run.sh",
        base_ref="main",
        image=TRIVY,
    )
    checks = ChecksSpec(
        repo="r", execution_id="e", checkout_dir="/c", command="make test", image=TRIVY
    )
    scripts = {
        "scanner": (scan_script(TRIVY, ["trivy", "fs", "/workspace"], PULLING), "buildah run"),
        "gate": (_run_script(spec, PULLING), "buildah run"),
        "checks": (_checks_script(checks, PULLING), "buildah run"),
        "fix": (_fix_script(checks, PULLING), "buildah run"),
    }

    for name, (script, run) in scripts.items():
        export, pull, remove = _pull_window(script)
        assert export < pull < remove < script.index(run), name
    assert "REGISTRY_AUTH_FILE" not in scan_script(TRIVY, ["trivy"])


async def test_a_scanner_pod_pulls_its_ecr_image_with_the_copied_token(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(registry_auth, "ecr_token", lambda account, region: "dG9rZW4=")
    monkeypatch.setattr(registry_auth.registry_mirrors, "current", lambda: None)
    ops = _CopyingOps()

    await PodCommandRunner(TRIVY, scanner="trivy", ops=ops)(
        ["trivy", "fs", str(tmp_path)], str(tmp_path)
    )

    assert json.loads(ops.copied[registry_auth.AUTH_DIR]["auth.json"][1])["auths"][ECR]
    assert f"export REGISTRY_AUTH_FILE={registry_auth.AUTH_FILE}" in ops.script
    assert "dG9rZW4=" not in ops.script
