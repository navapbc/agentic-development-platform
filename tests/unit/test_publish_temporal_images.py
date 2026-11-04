"""Tagging and publishing of the Temporal server and admin-tools images."""

from __future__ import annotations

import base64
import re
from pathlib import Path

import pytest
from scripts import publish_temporal_images as publisher

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRY = "111111111111.dkr.ecr.us-east-1.amazonaws.com"


def _digest_pin(text: str, image: str) -> str:
    match = re.search(rf"{re.escape(image)}:([0-9.]+@sha256:[0-9a-f]{{64}})", text)
    assert match, f"{image} is not pinned by version and digest"
    return match.group(1)


def _bundle_checksum(dockerfile: str) -> str:
    return re.search(r"^ARG RDS_CA_BUNDLE_SHA256=([0-9a-f]{64})$", dockerfile, re.M).group(1)


def test_the_admin_tools_image_pins_the_digest_compose_runs() -> None:
    compose = (REPO_ROOT / "compose.yaml").read_text(encoding="utf-8")
    dockerfile = (REPO_ROOT / "docker/temporal-admin-tools.Dockerfile").read_text(encoding="utf-8")

    assert _digest_pin(dockerfile, "temporalio/admin-tools") == _digest_pin(
        compose, "temporalio/admin-tools"
    )


def test_both_images_pin_the_same_rds_ca_bundle() -> None:
    server = (REPO_ROOT / "docker/temporal.Dockerfile").read_text(encoding="utf-8")
    admin_tools = (REPO_ROOT / "docker/temporal-admin-tools.Dockerfile").read_text(encoding="utf-8")

    assert _bundle_checksum(server) == _bundle_checksum(admin_tools)
    for dockerfile in (server, admin_tools):
        assert "ADD --checksum=sha256:${RDS_CA_BUNDLE_SHA256}" in dockerfile
        assert "/etc/ssl/rds-global-bundle.pem" in dockerfile


@pytest.mark.parametrize(
    ("spec", "version"),
    list(zip(publisher.IMAGES, ["1.32.1", "1.32.1", "2.58.0"], strict=True)),
    ids=lambda value: getattr(value, "name", value),
)
def test_the_tag_carries_the_upstream_version_and_architecture(spec, version) -> None:
    tag = publisher.image_tag(spec, REPO_ROOT, "arm64")

    assert re.fullmatch(rf"{re.escape(version)}-[0-9a-f]{{12}}-arm64", tag)


def test_the_ui_image_pins_the_digest_compose_runs() -> None:
    compose = (REPO_ROOT / "compose.yaml").read_text(encoding="utf-8")
    dockerfile = (REPO_ROOT / "docker/temporal-ui.Dockerfile").read_text(encoding="utf-8")

    assert _digest_pin(dockerfile, "temporalio/ui") == _digest_pin(compose, "temporalio/ui")


def test_the_server_config_template_matches_the_server_version() -> None:
    dockerfile = (REPO_ROOT / "docker/temporal.Dockerfile").read_text(encoding="utf-8")
    version = publisher.upstream_version(dockerfile)

    assert (
        f"/temporalio/temporal/v{version}/common/config/config_template_embedded.yaml" in dockerfile
    )


def _project(tmp_path: Path) -> Path:
    (tmp_path / "docker").mkdir()
    (tmp_path / "docker/temporal-admin-tools.Dockerfile").write_text(
        "ARG TEMPORAL_ADMIN_TOOLS_IMAGE=temporalio/admin-tools:1.32.1@sha256:" + "a" * 64 + "\n",
        encoding="utf-8",
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
    "reference",
    [
        "temporalio/server:1.32.1@sha256:" + "a" * 64,
        "docker.io/temporalio/server:1.32.1@sha256:" + "a" * 64,
        "mirror.example.com/temporalio/server:1.32.1@sha256:" + "a" * 64,
    ],
)
def test_the_version_is_read_with_or_without_a_registry(reference) -> None:
    assert publisher.upstream_version(f"ARG TEMPORAL_SERVER_IMAGE={reference}\n") == "1.32.1"


def test_a_dockerfile_without_a_pinned_upstream_image_is_refused() -> None:
    with pytest.raises(ValueError, match="pins no temporalio image"):
        publisher.upstream_version("FROM temporalio/server:latest\n")


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


def test_publish_builds_only_the_images_not_yet_pushed(monkeypatch) -> None:
    server_tag = publisher.image_tag(publisher.IMAGES[0], REPO_ROOT, "arm64")
    admin_tag = publisher.image_tag(ADMIN_TOOLS, REPO_ROOT, "arm64")
    ui_tag = publisher.image_tag(publisher.IMAGES[2], REPO_ROOT, "arm64")
    ecr = _FakeEcr(
        {
            ("agentic-platform/temporal-server", server_tag): "sha256:server",
            ("agentic-platform/temporal-ui", ui_tag): "sha256:ui",
        }
    )
    commands: list[tuple[list[str], dict]] = []

    def run(argv, *, timeout, **options):
        commands.append((list(argv), options))
        if argv[:3] == ["docker", "buildx", "build"]:
            ecr.pushed[("agentic-platform/temporal-admin-tools", admin_tag)] = "sha256:admin"

    monkeypatch.setattr(publisher.process, "run", run)

    references = publisher.publish(ecr, REGISTRY, "agentic-platform", "arm64", REPO_ROOT)

    assert references == [
        f"{REGISTRY}/agentic-platform/temporal-server:{server_tag}@sha256:server",
        f"{REGISTRY}/agentic-platform/temporal-admin-tools:{admin_tag}@sha256:admin",
        f"{REGISTRY}/agentic-platform/temporal-ui:{ui_tag}@sha256:ui",
    ]
    login, build = commands
    assert login[0] == ["docker", "login", "--username", "AWS", "--password-stdin", REGISTRY]
    assert login[1]["input"] == b"secret"
    assert build[0] == publisher.build_command(
        ADMIN_TOOLS,
        REPO_ROOT,
        f"{REGISTRY}/agentic-platform/temporal-admin-tools:{admin_tag}",
        "arm64",
    )


def test_publish_skips_the_login_when_every_image_is_pushed(monkeypatch) -> None:
    pushed = {
        (f"agentic-platform/{spec.name}", publisher.image_tag(spec, REPO_ROOT, "amd64")): "sha256:x"
        for spec in publisher.IMAGES
    }
    commands: list[list[str]] = []
    monkeypatch.setattr(publisher.process, "run", lambda argv, **_: commands.append(list(argv)))

    publisher.publish(_FakeEcr(pushed), REGISTRY, "agentic-platform", "amd64", REPO_ROOT)

    assert commands == []


def test_the_build_targets_the_requested_platform_without_attestations() -> None:
    command = publisher.build_command(ADMIN_TOOLS, REPO_ROOT, "registry/repo:tag", "amd64")

    assert command[command.index("--platform") + 1] == "linux/amd64"
    assert {"--provenance=false", "--sbom=false", "--push"} <= set(command)
    assert command[-1] == str(REPO_ROOT)
