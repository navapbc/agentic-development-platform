"""registries.yaml routes the gate, checks, and scanner pods' pulls and installs through mirrors."""

from __future__ import annotations

import base64
import json
import shlex
from pathlib import Path

import pytest
import yaml
from services.integration.credentials import config_version, current_config_version
from services.orchestration.gates import registry_auth, registry_mirrors

ECR = "123456789012.dkr.ecr.us-east-1.amazonaws.com"
ARTIFACTORY = "artifactory.example.gov"
EXTERNAL = {
    "mode": "external",
    "mirrors": {
        "docker.io": f"{ARTIFACTORY}/docker-remote",
        "ghcr.io": f"{ARTIFACTORY}/ghcr-remote",
    },
    "packages": {
        "pypi": f"https://{ARTIFACTORY}/artifactory/api/pypi/pypi-remote/simple",
        "npm": f"https://{ARTIFACTORY}/artifactory/api/npm/npm-remote/",
        "go": f"https://{ARTIFACTORY}/artifactory/api/go/go-remote",
    },
    "credential": {"username": "adp-reader", "token_env": "REGISTRY_TOKEN"},
}
AWS = {
    "mode": "aws",
    "mirrors": {
        "docker.io/library": f"{ECR}/ecr-public/docker/library",
        "quay.io": f"{ECR}/quay",
    },
}


class _Ops:
    def __init__(self) -> None:
        self.copied: dict[str, dict[str, str]] = {}

    async def copy_in(self, pod, namespace, source_dir, dest_dir):
        from pathlib import Path

        self.copied[dest_dir] = {p.name: p.read_text() for p in Path(source_dir).iterdir()}


def test_the_drop_in_sends_each_upstream_to_its_mirror() -> None:
    conf = registry_mirrors.registries_conf(registry_mirrors.parse(AWS))

    assert 'prefix = "docker.io/library"' in conf
    assert f'location = "{ECR}/ecr-public/docker/library"' in conf
    assert conf.count("[[registry.mirror]]") == 2


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"mode": "proxy"}, "mode must be one of"),
        ({"mode": "aws", "mirrors": {"docker.io": "https://x/y"}}, "without a scheme"),
        ({"mode": "aws", "packages": {"pypi": "https://x"}}, "public indexes"),
        ({"mode": "external", "packages": {"maven": "https://x"}}, "unknown package types"),
        ({"mode": "external"}, "credential.username and token_env"),
    ],
)
def test_a_malformed_config_is_refused(raw, message) -> None:
    with pytest.raises(registry_mirrors.RegistryConfigError, match=message):
        registry_mirrors.parse(raw)


async def test_aws_mode_pulls_the_mirrors_with_the_ecr_token_and_installs_no_package_config() -> (
    None
):
    ops = _Ops()

    setup = await registry_auth.install(
        ops,
        "pod",
        "ns",
        [],
        token=lambda account, region: "ZWNy",
        registries=registry_mirrors.parse(AWS),
    )

    auths = json.loads(ops.copied[registry_auth.AUTH_DIR]["auth.json"])["auths"]
    assert auths == {ECR: {"auth": "ZWNy"}}
    assert registry_auth.MIRRORS_FILE in ops.copied[registry_auth.MIRRORS_DIR]
    assert registry_auth.PACKAGE_AUTH_DIR not in ops.copied
    assert setup.authenticated and not setup.package_auth
    assert setup.run_flags() == "" and setup.build_flags() == ""


async def test_external_mode_authenticates_the_mirrors_and_the_package_installs(
    monkeypatch,
) -> None:
    monkeypatch.setenv("REGISTRY_TOKEN", "s3cr3t")
    ops = _Ops()

    setup = await registry_auth.install(
        ops, "pod", "ns", [], token=None, registries=registry_mirrors.parse(EXTERNAL)
    )

    auths = json.loads(ops.copied[registry_auth.AUTH_DIR]["auth.json"])["auths"]
    assert base64.b64decode(auths[ARTIFACTORY]["auth"]).decode() == "adp-reader:s3cr3t"
    files = ops.copied[registry_auth.PACKAGE_AUTH_DIR]
    assert f"machine {ARTIFACTORY}\nlogin adp-reader\npassword s3cr3t\n" == files["netrc"]
    assert files["npmrc"] == f"//{ARTIFACTORY}/artifactory/api/npm/npm-remote/:_authToken=s3cr3t\n"
    flags = shlex.split(setup.run_flags())
    assert "PIP_INDEX_URL=" + EXTERNAL["packages"]["pypi"] in flags
    assert f"NETRC={registry_auth.PACKAGE_AUTH_DIR}/netrc" in flags
    assert "s3cr3t" not in setup.run_flags() + setup.build_flags() + setup.before_pull()


async def test_external_mode_without_its_token_is_an_error(monkeypatch) -> None:
    monkeypatch.delenv("REGISTRY_TOKEN", raising=False)

    with pytest.raises(registry_auth.RegistryAuthError, match="REGISTRY_TOKEN"):
        await registry_auth.install(
            _Ops(), "pod", "ns", [], registries=registry_mirrors.parse(EXTERNAL)
        )


def test_the_checks_run_gets_the_package_indexes_and_the_engine_build_its_flags() -> None:
    from services.orchestration.checks.runner import ChecksSpec, _checks_script
    from services.orchestration.gates.pod_runner import PodEngineSpec, _run_script

    setup = registry_auth.RegistrySetup(
        authenticated=True, package_auth=True, packages={"PIP_INDEX_URL": "https://x/simple"}
    )
    checks = _checks_script(
        ChecksSpec(repo="r", execution_id="e", checkout_dir="/c", command="make", image="img"),
        setup,
    )
    engine = _run_script(
        PodEngineSpec(
            name="g", execution_id="e", checkout_dir="/c", entrypoint="run.sh", base_ref="main"
        ),
        setup,
    )

    assert f"-v {registry_auth.PACKAGE_AUTH_DIR}:{registry_auth.PACKAGE_AUTH_DIR}:ro" in checks
    assert "-e PIP_INDEX_URL=https://x/simple" in checks
    assert f"buildah bud --volume {registry_auth.PACKAGE_AUTH_DIR}" in engine
    assert "--env PIP_INDEX_URL=https://x/simple" in engine


def test_the_example_is_a_current_external_config() -> None:
    example = Path(__file__).resolve().parents[2] / "config" / "local" / "registries.yaml.example"
    raw = yaml.safe_load(example.read_text(encoding="utf-8"))

    config = registry_mirrors.parse(raw)

    assert config_version(raw) == current_config_version("registries")
    assert config.mode == registry_mirrors.EXTERNAL
    assert config.token_env == "REGISTRY_TOKEN"
    assert set(config.packages) == set(registry_mirrors.PACKAGE_ENV)
