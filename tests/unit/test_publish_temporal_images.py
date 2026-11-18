"""Tagging, scanning, and publishing of the Temporal server, admin-tools, and UI images."""

from __future__ import annotations

import base64
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts import publish_temporal_images as publisher

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRY = "111111111111.dkr.ecr.us-east-1.amazonaws.com"


def _bundle_checksum(dockerfile: str) -> str:
    return re.search(r"^ARG RDS_CA_BUNDLE_SHA256=([0-9a-f]{64})$", dockerfile, re.M).group(1)


def _dockerfile(name: str) -> str:
    return (REPO_ROOT / "docker" / name).read_text(encoding="utf-8")


def test_both_images_pin_the_same_rds_ca_bundle() -> None:
    server = _dockerfile("temporal.Dockerfile")
    admin_tools = _dockerfile("temporal-admin-tools.Dockerfile")

    assert _bundle_checksum(server) == _bundle_checksum(admin_tools)
    for dockerfile in (server, admin_tools):
        assert "ADD --checksum=sha256:${RDS_CA_BUNDLE_SHA256}" in dockerfile
        assert "/etc/ssl/rds-global-bundle.pem" in dockerfile


@pytest.mark.parametrize(
    ("spec", "version"),
    list(zip(publisher.IMAGES, ["1.32.2", "1.32.2", "2.58.1"], strict=True)),
    ids=lambda value: getattr(value, "name", value),
)
def test_the_tag_carries_the_source_version_and_architecture(spec, version) -> None:
    tag = publisher.image_tag(spec, REPO_ROOT, "arm64")

    assert re.fullmatch(rf"{re.escape(version)}-[0-9a-f]{{12}}-arm64", tag)


def test_the_server_and_admin_tools_build_the_same_release() -> None:
    assert publisher.source_version(_dockerfile("temporal.Dockerfile")) == (
        publisher.source_version(_dockerfile("temporal-admin-tools.Dockerfile"))
    )


@pytest.mark.parametrize("spec", publisher.IMAGES, ids=lambda spec: spec.name)
def test_every_binary_builds_with_the_validated_go_module(spec) -> None:
    dockerfile = _dockerfile(spec.dockerfile.removeprefix("docker/"))

    assert "ARG GOFIPS140=v1.0.0\n" in dockerfile
    builds = dockerfile.count("go build")
    assert builds and dockerfile.count('GOFIPS140="${GOFIPS140}" go build') == builds


def _project(tmp_path: Path) -> Path:
    (tmp_path / "docker").mkdir()
    (tmp_path / "docker/temporal-admin-tools.Dockerfile").write_text(
        "ARG TEMPORAL_VERSION=v1.32.2\n", encoding="utf-8"
    )
    (tmp_path / "docker/temporal-admin-tools.sh").write_text("echo schema\n", encoding="utf-8")
    return tmp_path


ADMIN_TOOLS = publisher.IMAGES[1]


def test_the_tag_changes_when_a_copied_file_changes(tmp_path) -> None:
    root = _project(tmp_path)
    before = publisher.image_tag(ADMIN_TOOLS, root, "arm64")

    (root / "docker/temporal-admin-tools.sh").write_text("echo namespace\n", encoding="utf-8")

    assert publisher.image_tag(ADMIN_TOOLS, root, "arm64") != before


def test_the_tag_is_stable_for_unchanged_inputs(tmp_path) -> None:
    root = _project(tmp_path)

    assert publisher.image_tag(ADMIN_TOOLS, root, "arm64") == publisher.image_tag(
        ADMIN_TOOLS, root, "arm64"
    )


@pytest.mark.parametrize(
    ("line", "version"),
    [("ARG TEMPORAL_VERSION=v1.32.2", "1.32.2"), ("ARG TEMPORAL_UI_VERSION=v2.58.1", "2.58.1")],
)
def test_the_version_is_read_from_the_source_tag(line, version) -> None:
    assert publisher.source_version(f"ARG TEMPORAL_CLI_VERSION=v1.10.0\n{line}\n") == version


def test_a_dockerfile_without_a_source_tag_is_refused() -> None:
    with pytest.raises(ValueError, match="pins no TEMPORAL_VERSION"):
        publisher.source_version("FROM temporalio/server:1.32.1\n")


class _ImageNotFound(Exception):
    pass


class _FakeEcr:
    class exceptions:
        ImageNotFoundException = _ImageNotFound

    def __init__(self, pushed: dict[tuple[str, str], str]) -> None:
        self.pushed = pushed

    def describe_images(self, repositoryName: str, imageIds: list[dict[str, str]]):
        key = (repositoryName, imageIds[0]["imageTag"])
        if key not in self.pushed:
            raise _ImageNotFound
        return {"imageDetails": [{"imageDigest": self.pushed[key]}]}

    def get_authorization_token(self):
        token = base64.b64encode(b"AWS:secret").decode()
        return {"authorizationData": [{"authorizationToken": token}]}


class _Scanner:
    def __init__(self, refused: set[str]) -> None:
        self.refused = refused
        self.scanned: list[str] = []

    def scan(self, image: str, keys: list[str]):
        self.scanned.append(image)
        passed = not any(name in image for name in self.refused)
        return SimpleNamespace(
            passed=passed, describe=lambda: f"{image} scanned", detail=lambda: []
        )


def _publisher(ecr, scanner, commands):
    from scripts.publish_platform_images import Publisher

    def run(argv, **options):
        commands.append(list(argv))
        if argv[:2] == ["docker", "push"]:
            image = argv[2]
            repository, _, tag = image.removeprefix(f"{REGISTRY}/").partition(":")
            ecr.pushed[(repository, tag)] = "sha256:" + repository.rsplit("/", 1)[1]
        return subprocess.CompletedProcess(argv, 0, "sha256:local\n", "")

    return Publisher(ecr, REGISTRY, "agentic-platform", "arm64", scanner, run, log=publisher.LOG)


def test_publish_scans_and_pushes_only_the_images_not_yet_pushed(monkeypatch) -> None:
    server_tag = publisher.image_tag(publisher.IMAGES[0], REPO_ROOT, "arm64")
    admin_tag = publisher.image_tag(ADMIN_TOOLS, REPO_ROOT, "arm64")
    ui_tag = publisher.image_tag(publisher.IMAGES[2], REPO_ROOT, "arm64")
    ecr = _FakeEcr(
        {
            ("agentic-platform/temporal-server", server_tag): "sha256:server",
            ("agentic-platform/temporal-ui", ui_tag): "sha256:ui",
        }
    )
    monkeypatch.setattr("scripts.publish_platform_images._docker_login", lambda ecr, registry: None)
    commands: list[list[str]] = []
    scanner = _Scanner(set())

    references = publisher.publish(_publisher(ecr, scanner, commands), REPO_ROOT)

    admin_image = f"{REGISTRY}/agentic-platform/temporal-admin-tools:{admin_tag}"
    assert references == [
        f"{REGISTRY}/agentic-platform/temporal-server:{server_tag}@sha256:server",
        f"{admin_image}@sha256:temporal-admin-tools",
        f"{REGISTRY}/agentic-platform/temporal-ui:{ui_tag}@sha256:ui",
    ]
    assert scanner.scanned == [admin_image]
    build = next(command for command in commands if command[:3] == ["docker", "buildx", "build"])
    assert build[build.index("--tag") + 1] == admin_image
    assert "--load" in build
    assert ["docker", "push", admin_image] in commands


def test_an_image_with_a_blocking_finding_is_not_pushed(monkeypatch) -> None:
    ecr = _FakeEcr({})
    monkeypatch.setattr("scripts.publish_platform_images._docker_login", lambda ecr, registry: None)
    commands: list[list[str]] = []
    platform = _publisher(ecr, _Scanner({"temporal-ui"}), commands)

    references = publisher.publish(platform, REPO_ROOT)

    assert references[2].endswith("@refused")
    assert not any(c[:2] == ["docker", "push"] and "temporal-ui" in c[2] for c in commands)
    assert len(platform.refused) == 1


def test_publish_skips_the_build_when_every_image_is_pushed() -> None:
    pushed = {
        (f"agentic-platform/{spec.name}", publisher.image_tag(spec, REPO_ROOT, "arm64")): "sha256:x"
        for spec in publisher.IMAGES
    }
    commands: list[list[str]] = []

    publisher.publish(_publisher(_FakeEcr(pushed), _Scanner(set()), commands), REPO_ROOT)

    assert not any(command[0] == "docker" for command in commands)


def test_the_build_targets_the_requested_platform_without_attestations() -> None:
    command = publisher.build_command(ADMIN_TOOLS, REPO_ROOT, "registry/repo:tag", "amd64")

    assert command[command.index("--platform") + 1] == "linux/amd64"
    assert {"--provenance=false", "--sbom=false", "--push"} <= set(command)
    assert command[-1] == str(REPO_ROOT)
