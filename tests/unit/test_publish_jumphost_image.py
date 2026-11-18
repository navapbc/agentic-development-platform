"""Publishing the jumphost image through the platform scan, CIS, push, and signing path."""

from __future__ import annotations

import base64
import contextlib
import json
import subprocess
from datetime import date
from pathlib import Path

import pytest
from scripts import publish_jumphost_image as jumphost
from scripts import publish_platform_images
from scripts.al2023_base import Base
from scripts.image_scan import ImageScanner
from services.integration.image_policy import FindingException

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRY = "111111111111.dkr.ecr.us-east-1.amazonaws.com"
LOCAL_DIGEST = "sha256:" + "a" * 64
PUSHED_DIGEST = "sha256:" + "b" * 64
IMAGE = f"{REGISTRY}/agentic-platform/jumphost-base:v1"
PROFILE = "arn:aws:signer:us-east-1:111111111111:/signing-profiles/platform"
BUILDER = f"{REGISTRY}/agentic-platform/buildah:0.3.0-0123456789ab-arm64@sha256:{'c' * 64}"


class _ImageNotFound(Exception):
    pass


class _FakeEcr:
    class exceptions:
        ImageNotFoundException = _ImageNotFound

    def __init__(self) -> None:
        self.pushed: dict[tuple[str, str], str] = {}

    def describe_images(self, repositoryName: str, imageIds: list[dict[str, str]]):  # noqa: N803
        key = (repositoryName, imageIds[0]["imageTag"])
        if key not in self.pushed:
            raise _ImageNotFound
        return {"imageDetails": [{"imageDigest": self.pushed[key]}]}

    def get_authorization_token(self):
        token = base64.b64encode(b"AWS:password").decode()
        return {"authorizationData": [{"authorizationToken": token}]}


class _Inspector:
    def __init__(self, vulnerabilities: list[dict]) -> None:
        self.vulnerabilities = vulnerabilities

    def scan_sbom(self, sbom, outputFormat):  # noqa: N803
        return {"sbom": {"vulnerabilities": self.vulnerabilities}}


class _Session:
    region_name = "us-east-1"

    def __init__(self, inspector: _Inspector) -> None:
        self._inspector = inspector

    def client(self, name: str):
        assert name == "inspector-scan"
        return self._inspector

    def get_credentials(self):
        return self

    def get_frozen_credentials(self):
        return self

    access_key, secret_key, token = "access", "secret", "session"


class _Docker:
    """Answer the docker commands a publish runs, holding a pushed image in the fake registry."""

    def __init__(self, ecr: _FakeEcr, blocking_cis_rules: str = "", signed: tuple = ()) -> None:
        self.ecr = ecr
        self.blocking_cis_rules = blocking_cis_rules
        self.signed = signed
        self.commands: list[list[str]] = []

    def __call__(self, command, **_):
        self.commands.append(list(command))
        stdout = ""
        if command[:3] == ["docker", "image", "inspect"]:
            stdout = '["PATH=/usr/bin:/bin"]' if "{{json .Config.Env}}" in command else LOCAL_DIGEST
        elif command[:2] == ["docker", "push"]:
            repository, _, tag = command[2].removeprefix(f"{REGISTRY}/").partition(":")
            self.ecr.pushed[(repository, tag)] = PUSHED_DIGEST
        elif "--entrypoint" in command and command[-2] == "list":
            stdout = (
                "└── application/vnd.cncf.notary.signature" if command[-1] in self.signed else ""
            )
        elif command[:2] == ["docker", "run"] and "--entrypoint" not in command:
            work = Path(command[command.index("--volume") + 1].split(":", 1)[0])
            (work / "sbom.json").write_text(json.dumps({"components": [1]}))
        elif command[:3] == ["docker", "buildx", "build"] and "--output" in command:
            destination = Path(command[command.index("--output") + 1].split("dest=", 1)[1])
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "results.tsv").write_text("xccdf_rule_1\tpass\n")
            (destination / "blocking.tsv").write_text(self.blocking_cis_rules)
        return subprocess.CompletedProcess(command, 0, stdout, "")

    def pushes(self) -> list[list[str]]:
        return [command for command in self.commands if command[:2] == ["docker", "push"]]

    def platform(self, command: list[str]) -> str:
        return command[command.index("--platform") + 1]

    def notation(self, verb: str) -> list[list[str]]:
        return [
            command[command.index("--entrypoint") + 3 :]
            for command in self.commands
            if "--entrypoint" in command and command[command.index("--entrypoint") + 3] == verb
        ]


@pytest.fixture(autouse=True)
def _offline(monkeypatch) -> None:
    from services.orchestration.gates import registry_auth

    monkeypatch.setattr(jumphost, "build_context", lambda root: contextlib.nullcontext(REPO_ROOT))
    monkeypatch.setattr(publish_platform_images, "_docker_login", lambda ecr, registry: None)
    monkeypatch.setattr(registry_auth, "ecr_token", lambda account, region: "dG9rZW4=")


def _publish(
    tmp_path: Path,
    vulnerabilities=(),
    exceptions=(),
    blocking_cis_rules: str = "",
    arch: str = "arm64",
    existing_digest: str = "",
    signed: tuple = (),
    unsigned: bool = False,
) -> tuple[int, _Docker]:
    ecr = _FakeEcr()
    if existing_digest:
        ecr.pushed[("agentic-platform/jumphost-base", "v1")] = existing_digest
    docker = _Docker(ecr, blocking_cis_rules, signed)
    session = _Session(_Inspector(list(vulnerabilities)))
    scanner = ImageScanner(session, arch, exceptions=exceptions, run=docker)
    scanner._scanner = "inspector-sbomgen:1"
    name_prefix, spec = jumphost.image_spec(jumphost.DEFAULT_REPOSITORY)
    publisher = publish_platform_images.Publisher(
        ecr,
        REGISTRY,
        name_prefix,
        arch,
        scanner,
        docker,
        log=jumphost.LOG,
        base=Base(arch, docker, evidence=tmp_path),
    )
    signer = (
        None if unsigned else jumphost.make_signer(session, ecr, REGISTRY, PROFILE, BUILDER, docker)
    )
    return jumphost.publish_signed(publisher, signer, spec, "v1", BUILDER), docker


HIGH_FINDING = {"id": "CVE-2026-0001", "severity": "high"}


@pytest.mark.parametrize(
    ("vulnerabilities", "blocking_cis_rules"),
    [
        ([HIGH_FINDING], ""),
        ([{"id": "CVE-2026-0002", "severity": "critical"}], ""),
        ([], "xccdf_rule_2\tfail\n"),
    ],
    ids=["high-finding", "critical-finding", "failed-cis-rule"],
)
def test_a_blocking_finding_or_a_failed_cis_rule_stops_the_push_and_the_signing(
    tmp_path, capsys, vulnerabilities, blocking_cis_rules
) -> None:
    status, docker = _publish(
        tmp_path, vulnerabilities=vulnerabilities, blocking_cis_rules=blocking_cis_rules
    )

    assert status == 1
    assert docker.pushes() == []
    assert docker.notation("sign") == []
    assert "not pushed" in capsys.readouterr().out


def test_a_clean_image_is_pushed_signed_and_its_cis_results_kept(tmp_path) -> None:
    status, docker = _publish(tmp_path)

    assert status == 0
    build = next(command for command in docker.commands if "--load" in command)
    assert build[build.index("--file") + 1] == str(REPO_ROOT / "docker/jumphost-base/Dockerfile")
    assert any(part.startswith("AL2023_IMAGE=agentic-dev-platform-al2023-base:") for part in build)
    assert docker.pushes() == [["docker", "push", IMAGE]]
    assert docker.notation("sign") == [
        [
            "sign",
            "--plugin",
            publish_platform_images.SIGNER_PLUGIN,
            "--id",
            PROFILE,
            f"{IMAGE}@{PUSHED_DIGEST}",
        ]
    ]
    assert (tmp_path / "jumphost-base" / "results.tsv").is_file()
    assert (tmp_path / "al2023-base" / "results.tsv").is_file()


@pytest.mark.parametrize("unsigned", [False, True], ids=["signing", "unsigned"])
def test_an_existing_tag_without_a_signature_is_refused_unscanned(
    tmp_path, capsys, unsigned
) -> None:
    status, docker = _publish(
        tmp_path,
        vulnerabilities=[{"id": "CVE-2026-0002", "severity": "critical"}],
        existing_digest=PUSHED_DIGEST,
        unsigned=unsigned,
    )

    assert status == 1
    assert not any("--load" in command for command in docker.commands)
    assert docker.pushes() == []
    assert docker.notation("sign") == []
    assert "Publish under a new --tag" in capsys.readouterr().out


def test_an_existing_signed_tag_is_accepted_without_signing_again(tmp_path, capsys) -> None:
    reference = f"{IMAGE}@{PUSHED_DIGEST}"

    status, docker = _publish(tmp_path, existing_digest=PUSHED_DIGEST, signed=(reference,))

    assert status == 0
    assert docker.pushes() == []
    assert docker.notation("list") == [["list", reference]]
    assert docker.notation("sign") == []
    output = capsys.readouterr().out
    assert f"{reference} is already pushed and signed" in output
    assert output.rstrip().endswith(reference)


def test_the_signer_runs_the_builder_on_its_own_architecture(tmp_path) -> None:
    status, docker = _publish(tmp_path, arch="amd64")

    assert status == 0
    build = next(command for command in docker.commands if "--load" in command)
    assert docker.platform(build) == "linux/amd64"
    notation = [command for command in docker.commands if "--entrypoint" in command]
    assert notation and {docker.platform(command) for command in notation} == {"linux/arm64"}


def test_a_finding_the_image_exceptions_accept_does_not_stop_the_push(tmp_path) -> None:
    accepted = FindingException(
        f"{REGISTRY}/agentic-platform/jumphost-base@{LOCAL_DIGEST}",
        HIGH_FINDING["id"],
        "No fixed package is published.",
        date(2099, 1, 1),
    )

    status, docker = _publish(tmp_path, vulnerabilities=[HIGH_FINDING], exceptions=[accepted])

    assert status == 0
    assert docker.pushes() == [["docker", "push", IMAGE]]


def test_a_repository_without_a_prefix_publishes_under_its_own_name() -> None:
    name_prefix, spec = jumphost.image_spec("jumphost")

    publisher = publish_platform_images.Publisher(None, REGISTRY, name_prefix, "arm64")

    assert publisher._repository(spec.name) == "jumphost"


@pytest.mark.parametrize(
    ("profile", "images", "message"),
    [
        ("", {}, "no signing profile"),
        (PROFILE, {}, "no builder image to sign in"),
        (
            PROFILE,
            {"eks_buildah_image": f"{REGISTRY}/agentic-platform/buildah@sha256:{'c' * 64}"},
            "names no architecture",
        ),
    ],
)
def test_a_publish_that_cannot_sign_stops_before_any_aws_call(
    monkeypatch, tmp_path, capsys, profile, images, message
) -> None:
    images_file = tmp_path / "images.auto.tfvars.json"
    images_file.write_text(json.dumps(images))
    monkeypatch.setattr(jumphost, "IMAGES_FILE", images_file)
    monkeypatch.setattr(jumphost, "signing_profile", lambda explicit: profile)
    monkeypatch.setattr(
        "scripts.config.get_settings",
        lambda: (_ for _ in ()).throw(AssertionError("no AWS call without signing")),
    )

    assert jumphost.main(["--tag", "v2"]) == 1
    assert message in capsys.readouterr().out


def test_the_builder_defaults_to_the_published_buildah_image(tmp_path) -> None:
    images_file = tmp_path / "images.auto.tfvars.json"
    images_file.write_text(json.dumps({"eks_buildah_image": BUILDER}))

    assert jumphost.signing_builder("", images_file) == BUILDER
    assert jumphost.signing_builder("explicit:1", images_file) == "explicit:1"
