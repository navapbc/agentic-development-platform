"""Building, tagging, and mirroring the images the platform ECS services run."""

from __future__ import annotations

import base64
import hashlib
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


def test_the_collector_and_opa_versions_come_from_their_pinned_bases() -> None:
    collector = (REPO_ROOT / "docker/otel-collector.Dockerfile").read_text(encoding="utf-8")
    opa = (REPO_ROOT / "docker/opa.Dockerfile").read_text(encoding="utf-8")

    assert publisher.base_image_version(collector, "opentelemetry-collector-contrib") == "0.160.0"
    assert publisher.base_image_version(opa, "openpolicyagent/opa") == "1.20.1"


def test_a_base_image_without_a_digest_is_refused() -> None:
    with pytest.raises(publisher.PublishError, match="pins no openpolicyagent/opa image"):
        publisher.base_image_version("FROM openpolicyagent/opa:1.20.1\n", "openpolicyagent/opa")


def test_every_cluster_mirror_reads_the_digest_its_setting_or_manifest_pins() -> None:
    from scripts.config import Settings

    mirrors = {mirror.name: mirror for mirror in publisher.cluster_mirrors(REPO_ROOT)}

    assert set(mirrors) == {*publisher.MIRRORED_SETTINGS, publisher.LITELLM}
    for name, field in publisher.MIRRORED_SETTINGS.items():
        mirror = mirrors[name]
        assert (
            Settings.model_fields[field].default == f"{mirror.source}:{mirror.tag}@{mirror.digest}"
        )
    litellm = mirrors[publisher.LITELLM]
    manifest = (REPO_ROOT / publisher.LITELLM_MANIFEST).read_text(encoding="utf-8")
    assert f"image: {litellm.source}:{litellm.tag}@{litellm.digest}" in manifest


def test_a_litellm_manifest_without_the_container_is_refused() -> None:
    manifest = (
        "kind: Deployment\nspec:\n  template:\n    spec:\n"
        "      containers:\n        - name: other\n          image: x\n"
    )

    with pytest.raises(publisher.PublishError, match="has no litellm container"):
        publisher.litellm_reference(manifest)


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
    assert platform.pushed == []


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
    assert platform.pushed == [reference]
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


def test_a_cluster_mirror_whose_digest_differs_names_the_setting_that_pins_it(commands) -> None:
    trivy = next(m for m in publisher.cluster_mirrors(REPO_ROOT) if m.name == "trivy")
    ecr = _FakeEcr({("agentic-platform/trivy", trivy.tag): "sha256:" + "c" * 64})

    with pytest.raises(publisher.PublishError, match="the trivy_image setting pins"):
        publisher.Publisher(ecr, REGISTRY, "agentic-platform", "arm64").mirror(trivy)


def test_a_publish_writes_every_composition_root_variable(commands, monkeypatch) -> None:
    monkeypatch.setattr(publisher, "generate_build_id", lambda: "0.3.0.20261105120000")
    compose = (REPO_ROOT / "compose.yaml").read_text(encoding="utf-8")
    ecr = _FakeEcr(
        {
            (f"agentic-platform/{mirror.name}", mirror.tag): mirror.digest
            for mirror in publisher.mirrors(compose) + publisher.cluster_mirrors(REPO_ROOT)
        }
    )

    def _run(command, **_):
        if command[0] == "git":
            return subprocess.run(command, capture_output=True, check=True)
        commands.append(command)
        _pushes(ecr, commands)
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(publisher.process, "run", _run)
    platform = publisher.Publisher(ecr, REGISTRY, "agentic-platform", "arm64")

    references = publisher.publish(platform, publisher.AcliBuild())

    values = publisher.tfvars(references)
    assert set(values) == set(publisher.TFVARS_KEYS.values()) | {
        publisher.GATE_ENGINE_IMAGES_KEY,
        publisher.RUNTIME_IMAGES_KEY,
    }
    assert set(values[publisher.GATE_ENGINE_IMAGES_KEY]) == {"security-policy", "test-policy"}
    assert set(values[publisher.RUNTIME_IMAGES_KEY]) == {"agent-server-jvm", "agent-server-go"}
    built = {
        command[command.index("--tag") + 1].split(":")[0]
        for command in commands
        if "--tag" in command
    }
    assert f"{REGISTRY}/agentic-platform/opa" in built


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


PUSHED = f"{REGISTRY}/agentic-platform/platform:0.3.0.20261105120000-abc-arm64@{DIGEST}"
REUSED = f"{REGISTRY}/agentic-platform/clickhouse:26.9@sha256:{'c' * 64}"


def test_the_references_map_to_the_composition_root_variables() -> None:
    assert publisher.tfvars([PUSHED, REUSED]) == {
        "container_platform_image": PUSHED,
        "container_clickhouse_image": REUSED,
    }


def test_every_published_image_has_a_composition_root_variable() -> None:
    from scripts.publish_temporal_images import ImageSpec

    published = (
        {spec.name for spec in vars(publisher).values() if isinstance(spec, ImageSpec)}
        | set(publisher.MIRRORED_SERVICES.values())
        | set(publisher.MIRRORED_SETTINGS)
        | {publisher.LITELLM}
    )

    assert published == set(publisher.TFVARS_KEYS)
    variables = (REPO_ROOT / "infra/terraform/examples/private-bedrock/variables.tf").read_text()
    assert all(f'variable "{key}"' in variables for key in publisher.TFVARS_KEYS.values())


def test_the_gate_engine_images_map_to_their_engine_names() -> None:
    engine = f"{REGISTRY}/agentic-platform/gate-engine-test-policy:0.3.0-abc-arm64@{DIGEST}"

    assert publisher.tfvars([PUSHED, engine]) == {
        "container_platform_image": PUSHED,
        "eks_gate_engine_images": {"test-policy": engine},
    }
    variables = (REPO_ROOT / "infra/terraform/examples/private-bedrock/variables.tf").read_text()
    assert f'variable "{publisher.GATE_ENGINE_IMAGES_KEY}"' in variables


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_each_gate_engine_builds_from_its_own_directory(monkeypatch) -> None:
    commands: list[list[str]] = []
    contexts: list[set[str]] = []

    def _build(command, **_):
        if command[:3] == ["docker", "buildx", "imagetools"]:
            return subprocess.CompletedProcess(command, 0, b'{"manifest": 1}', b"")
        commands.append(command)
        contexts.append({p.name for p in Path(command[-1]).iterdir()})
        return subprocess.CompletedProcess(command, 0, b"", b"")

    pushed = publisher.push_engines_locally("localhost:5001", "arm64", REPO_ROOT, _build)

    digest = hashlib.sha256(b'{"manifest": 1}').hexdigest()
    assert set(pushed) == {"security-policy", "test-policy"}
    assert all(image.endswith(f"@sha256:{digest}") for image in pushed.values())
    for command in commands:
        context = Path(command[-1])
        assert context.name in pushed
        assert command[command.index("--file") + 1] == str(context / "Dockerfile")
        assert "--push" in command
    assert all({"Dockerfile", "bin", "policy.py", "rules.toml"} <= names for names in contexts)
    assert pushed["test-policy"].startswith("localhost:5001/gate-engine-test-policy:")


def test_the_local_engine_references_name_the_registry_as_the_pods_address_it() -> None:
    from scripts.gate_engine_images import pull_references

    pushed = {"security-policy": "localhost:5001/gate-engine-security-policy:1"}

    assert pull_references(pushed, "localhost:5001", "host.docker.internal:5001") == {
        "security-policy": "host.docker.internal:5001/gate-engine-security-policy:1"
    }


def test_the_manifest_marks_only_the_images_this_run_pushed() -> None:
    entries = publisher.manifest([PUSHED, REUSED], [PUSHED])

    assert entries == [
        {
            "name": "platform",
            "repository": f"{REGISTRY}/agentic-platform/platform",
            "digest": DIGEST,
            "reference": PUSHED,
            "pushed": True,
        },
        {
            "name": "clickhouse",
            "repository": f"{REGISTRY}/agentic-platform/clickhouse",
            "digest": f"sha256:{'c' * 64}",
            "reference": REUSED,
            "pushed": False,
        },
    ]


def test_a_local_build_loads_each_image_with_the_publish_command(commands) -> None:
    publisher.build_locally([(publisher.OPA, "agentic-dev-platform-opa:1", {})], "arm64", REPO_ROOT)

    assert commands[-1][:3] == ["docker", "buildx", "build"]
    assert "--load" in commands[-1] and "--push" not in commands[-1]
    assert commands[-1][commands[-1].index("--tag") + 1] == "agentic-dev-platform-opa:1"


def test_the_images_file_is_ignored_by_git() -> None:
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", str(publisher.IMAGES_FILE)], cwd=REPO_ROOT, check=False
    )

    assert ignored.returncode == 0


def test_the_catalog_sandbox_images_build_on_the_pinned_agent_server_image() -> None:
    from scripts.config import Settings

    pinned = Settings.model_fields["openhands_agent_server_image"].default
    for spec in publisher.RUNTIME_IMAGES.values():
        dockerfile = (REPO_ROOT / spec.dockerfile).read_text(encoding="utf-8")
        assert f"\nFROM {pinned}\n" in dockerfile, spec.dockerfile


def test_the_catalog_sandbox_images_use_the_toolchain_of_their_profiles_checks() -> None:
    import yaml

    catalog = yaml.safe_load((REPO_ROOT / "config/platform/runtime-profiles.yaml").read_text())
    for profile, image in (("java-spring", "agent-server-jvm"), ("go-service", "agent-server-go")):
        checks = catalog["profiles"][profile]["checks"]["image"].removeprefix("docker.io/library/")
        dockerfile = (REPO_ROOT / f"docker/{image}.Dockerfile").read_text(encoding="utf-8")
        assert f"public.ecr.aws/docker/library/{checks} AS" in dockerfile, image


class _Credentials:
    access_key, secret_key, token = "AKIA", "secret", "session"

    def get_frozen_credentials(self):
        return self


class _Session:
    region_name = "us-east-1"

    def get_credentials(self):
        return _Credentials()


def test_the_catalog_images_are_signed_once_inside_the_builder_image(monkeypatch) -> None:
    from services.orchestration.gates import registry_auth

    monkeypatch.setattr(registry_auth, "ecr_token", lambda account, region: "dG9rZW4=")
    registry = "123456789012.dkr.ecr.us-east-1.amazonaws.com"
    signed_before = f"{registry}/agentic-platform/semgrep:1@sha256:{'a' * 64}"
    unsigned = f"{registry}/agentic-platform/gate-engine-test-policy:1@sha256:{'b' * 64}"
    platform = f"{registry}/agentic-platform/platform:1@sha256:{'c' * 64}"
    builder = f"{registry}/agentic-platform/buildah:1@sha256:{'d' * 64}"
    calls: list[list[str]] = []
    env_files: list[str] = []

    def run(command, **_):
        calls.append(command)
        env_files.append(Path(command[command.index("--env-file") + 1]).read_text())
        listing = "└── application/vnd.cncf.notary.signature" if signed_before in command else ""
        return subprocess.CompletedProcess(command, 0, listing, "")

    signer = publisher.Signer(_Session(), None, registry, "arn:profile", "arm64", run)
    signed = signer.sign([signed_before, unsigned, platform], builder)

    assert signed == [unsigned]
    sign = next(command for command in calls if "sign" in command)
    assert sign[sign.index("--entrypoint") + 1 :] == [
        "notation",
        builder,
        "sign",
        "--plugin",
        "com.amazonaws.signer.notation.plugin",
        "--id",
        "arn:profile",
        unsigned,
    ]
    assert not any(platform in command for command in calls)
    assert all("AWS_SECRET_ACCESS_KEY=secret" in env for env in env_files)
    assert not any("secret" in part for command in calls for part in command)


def test_publishing_without_a_signing_profile_stops_before_any_aws_call(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(publisher, "signing_profile", lambda explicit: "")
    monkeypatch.setattr(
        "scripts.config.get_settings",
        lambda: (_ for _ in ()).throw(AssertionError("no AWS call without a signing profile")),
    )

    assert publisher.main([]) == 1
    assert "no signing profile, so nothing is published" in capsys.readouterr().out


def test_unsigned_publishing_proceeds_without_a_signing_profile(monkeypatch) -> None:
    monkeypatch.setattr(publisher, "signing_profile", lambda explicit: "")

    class _Reached(Exception):
        pass

    def reached(settings):
        raise _Reached

    monkeypatch.setattr("scripts.aws_clients.configure_aws_fips", reached)

    with pytest.raises(_Reached):
        publisher.main(["--unsigned"])
