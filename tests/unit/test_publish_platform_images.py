"""Building, tagging, and mirroring the images the platform ECS services run."""

from __future__ import annotations

import base64
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from scripts import publish_platform_images as publisher

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRY = "111111111111.dkr.ecr.us-east-1.amazonaws.com"
DIGEST = "sha256:" + "a" * 64


class _ImageNotFound(Exception):
    pass


class _Paginator:
    def __init__(self, ecr: _FakeEcr) -> None:
        self._ecr = ecr

    def paginate(self, repositoryName: str, filter: dict[str, str]):
        details = [
            {"imageDigest": digest, "imageTags": [tag]}
            for (repository, tag), digest in self._ecr.pushed.items()
            if repository == repositoryName
        ]
        yield {"imageDetails": details}


class _FakeEcr:
    class exceptions:
        ImageNotFoundException = _ImageNotFound

    def __init__(self, pushed: dict[tuple[str, str], str] | None = None) -> None:
        self.pushed = dict(pushed or {})

    def describe_images(self, repositoryName: str, imageIds: list[dict[str, str]]):
        key = (repositoryName, imageIds[0]["imageTag"])
        if key not in self.pushed:
            raise _ImageNotFound
        return {"imageDetails": [{"imageDigest": self.pushed[key]}]}

    def get_paginator(self, name: str) -> _Paginator:
        assert name == "describe_images"
        return _Paginator(self)

    def get_authorization_token(self):
        token = base64.b64encode(b"AWS:secret").decode()
        return {"authorizationData": [{"authorizationToken": token}]}


@pytest.fixture
def commands(monkeypatch) -> list[list[str]]:
    """Record every command, pushing the image a build or copy names with a fixed digest."""
    recorded: list[list[str]] = []

    def _run(command, **_):
        recorded.append(command)
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(publisher.process, "run", _run)
    return recorded


def _pushes(ecr: _FakeEcr, commands: list[list[str]]) -> None:
    for command in commands:
        if "--tag" in command:
            image = command[command.index("--tag") + 1]
            repository, _, tag = image.removeprefix(f"{REGISTRY}/").partition(":")
            ecr.pushed[(repository, tag)] = DIGEST


def test_every_mirror_reads_the_digest_compose_pins() -> None:
    compose = (REPO_ROOT / "compose.yaml").read_text(encoding="utf-8")

    mirrors = {mirror.name: mirror for mirror in publisher.mirrors(compose)}

    assert set(mirrors) == {"langfuse-web", "langfuse-worker", "clickhouse"}
    for name, service in publisher.MIRRORED_SERVICES.items():
        mirror = mirrors[name]
        assert f"image: {mirror.source}:{mirror.tag}@{mirror.digest}" in compose, service


def test_a_compose_image_without_a_digest_is_refused() -> None:
    compose = (
        "services:\n  langfuse-web:\n    image: docker.langfuse.com/langfuse/langfuse:latest\n"
        "  langfuse-worker:\n    image: x\n  clickhouse:\n    image: y\n"
    )

    with pytest.raises(publisher.PublishError, match="without a tag and digest"):
        publisher.mirrors(compose)


def test_the_collector_version_comes_from_its_pinned_base() -> None:
    dockerfile = (REPO_ROOT / "docker/otel-collector.Dockerfile").read_text(encoding="utf-8")

    assert publisher.collector_version(dockerfile) == "0.160.0"


def test_the_platform_image_pins_the_rds_ca_bundle_the_temporal_images_pin() -> None:
    def checksum(path: str) -> str:
        text = (REPO_ROOT / path).read_text(encoding="utf-8")
        return re.search(r"^ARG RDS_CA_BUNDLE_SHA256=([0-9a-f]{64})$", text, re.M).group(1)

    platform = (REPO_ROOT / "docker/Dockerfile").read_text(encoding="utf-8")

    assert "ADD --checksum=sha256:${RDS_CA_BUNDLE_SHA256} --chmod=0444" in platform
    assert checksum("docker/Dockerfile") == checksum("docker/temporal.Dockerfile")


def test_the_source_hash_follows_the_acli_build_and_the_architecture() -> None:
    plain = publisher.platform_source_hash(REPO_ROOT, publisher.AcliBuild(), "arm64")

    assert plain == publisher.platform_source_hash(REPO_ROOT, publisher.AcliBuild(), "arm64")
    assert plain != publisher.platform_source_hash(REPO_ROOT, publisher.AcliBuild(), "amd64")
    assert plain != publisher.platform_source_hash(
        REPO_ROOT, publisher.AcliBuild(True, "1.2.3", "b" * 64), "arm64"
    )


def test_sources_already_published_reuse_their_image_and_build_id(commands) -> None:
    source_hash = publisher.platform_source_hash(REPO_ROOT, publisher.AcliBuild(), "arm64")
    tag = f"0.3.0.20261101000000-{source_hash}-arm64"
    ecr = _FakeEcr({("agentic-platform/platform", tag): DIGEST})
    platform = publisher.Publisher(ecr, REGISTRY, "agentic-platform", "arm64")

    reference = platform.platform(REPO_ROOT, publisher.AcliBuild())

    assert reference == f"{REGISTRY}/agentic-platform/platform:{tag}@{DIGEST}"
    assert commands == []


def test_new_sources_mint_a_build_id_and_bake_it_into_the_image(commands, monkeypatch) -> None:
    monkeypatch.setattr(publisher, "generate_build_id", lambda: "0.3.0.20261105120000")
    ecr = _FakeEcr()
    platform = publisher.Publisher(ecr, REGISTRY, "agentic-platform", "arm64")
    acli = publisher.AcliBuild(True, "1.2.3", "b" * 64)

    def _run(command, **_):
        commands.append(command)
        _pushes(ecr, commands)
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(publisher.process, "run", _run)
    reference = platform.platform(REPO_ROOT, acli)

    build = commands[-1]
    assert build[:3] == ["docker", "buildx", "build"]
    arguments = [build[index + 1] for index, part in enumerate(build) if part == "--build-arg"]
    assert arguments == [
        "AGENT_BUILD_ID=0.3.0.20261105120000",
        "INSTALL_ACLI=true",
        "ACLI_VERSION=1.2.3",
        f"ACLI_SHA256={'b' * 64}",
    ]
    tag = reference.split(":")[1].split("@")[0]
    assert publisher.build_id_of(tag) == "0.3.0.20261105120000"
    assert re.fullmatch(r"0\.3\.0\.20261105120000-[0-9a-f]{12}-arm64", tag)


def test_a_mirror_keeps_the_pinned_digest(commands, monkeypatch) -> None:
    ecr = _FakeEcr()
    mirror = publisher.Mirror(
        "clickhouse", "docker.io/clickhouse/clickhouse-server", "26.9", DIGEST
    )

    def _run(command, **_):
        commands.append(command)
        _pushes(ecr, commands)
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(publisher.process, "run", _run)
    reference = publisher.Publisher(ecr, REGISTRY, "agentic-platform", "arm64").mirror(mirror)

    assert commands[-1][:5] == ["docker", "buildx", "imagetools", "create", "--tag"]
    assert commands[-1][-1] == f"docker.io/clickhouse/clickhouse-server@{DIGEST}"
    assert reference == f"{REGISTRY}/agentic-platform/clickhouse:26.9@{DIGEST}"


def test_a_mirror_whose_digest_differs_from_the_pin_is_refused(commands) -> None:
    other = "sha256:" + "c" * 64
    ecr = _FakeEcr({("agentic-platform/clickhouse", "26.9"): other})
    mirror = publisher.Mirror(
        "clickhouse", "docker.io/clickhouse/clickhouse-server", "26.9", DIGEST
    )

    with pytest.raises(publisher.PublishError, match="compose.yaml pins"):
        publisher.Publisher(ecr, REGISTRY, "agentic-platform", "arm64").mirror(mirror)


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_the_build_context_holds_no_ignored_file(tmp_path) -> None:
    repository = tmp_path / "repository"
    (repository / "vendor/bin").mkdir(parents=True)
    (repository / ".gitignore").write_text("vendor/bin/*\n!vendor/bin/.gitkeep\n", encoding="utf-8")
    (repository / "vendor/bin/.gitkeep").write_text("", encoding="utf-8")
    (repository / "vendor/bin/acli").write_text("darwin binary", encoding="utf-8")
    (repository / "tracked.py").write_text("print(1)\n", encoding="utf-8")
    (repository / "untracked.py").write_text("print(2)\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "add", ".gitignore", "tracked.py", "vendor"], check=True
    )

    with publisher.build_context(repository) as context:
        files = sorted(
            path.relative_to(context).as_posix() for path in context.rglob("*") if path.is_file()
        )

    assert files == [".gitignore", "tracked.py", "untracked.py", "vendor/bin/.gitkeep"]
