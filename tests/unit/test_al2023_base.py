"""The hardened base, the build argument that starts images from it, and the CIS scan gate."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from scripts import al2023_base
from scripts.publish_temporal_images import HARDENED_BASE_FILES, ImageSpec, content_hash

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("path", "problems"),
    [
        ("/usr/local/bin:/usr/bin:/bin", []),
        ("/usr/bin::/bin", ["PATH entry '' is empty"]),
        (".:/usr/bin", ["PATH entry '.' is relative"]),
        ("/usr/bin:bin", ["PATH entry 'bin' is relative"]),
    ],
)
def test_a_path_with_an_empty_or_relative_entry_is_a_problem(path, problems) -> None:
    assert al2023_base.path_problems(path) == problems


def test_the_accepted_rules_each_carry_a_reason() -> None:
    text = (REPO_ROOT / "docker/al2023/cis-accepted.txt").read_text(encoding="utf-8")

    entries = [line.split(maxsplit=1) for line in text.splitlines() if line and line[0] != "#"]
    assert all(len(entry) == 2 and entry[1].strip() for entry in entries)


def _project(tmp_path: Path, dockerfile: str) -> Path:
    for name in HARDENED_BASE_FILES:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("base\n", encoding="utf-8")
    (tmp_path / "engine").mkdir()
    (tmp_path / "engine/Dockerfile").write_text(dockerfile, encoding="utf-8")
    return tmp_path


def test_a_base_change_retags_an_image_that_starts_from_it(tmp_path) -> None:
    root = _project(tmp_path, "ARG AL2023_IMAGE=x\nFROM ${AL2023_IMAGE}\n")
    spec = ImageSpec("engine", "Dockerfile", ())
    before = content_hash(spec, root / "engine")

    (root / "docker/al2023/cis-accepted.txt").write_text("changed\n", encoding="utf-8")

    assert content_hash(spec, root / "engine") != before


def test_a_base_change_leaves_an_image_on_another_base_alone(tmp_path) -> None:
    root = _project(tmp_path, "FROM scratch\n")
    spec = ImageSpec("engine", "Dockerfile", ())
    before = content_hash(spec, root / "engine")

    (root / "docker/al2023/cis-accepted.txt").write_text("changed\n", encoding="utf-8")

    assert content_hash(spec, root / "engine") == before


def _recorder(present: bool = True, env: str = '["PATH=/usr/bin:/bin"]'):
    commands: list[list[str]] = []

    def run(command, **_):
        commands.append(list(command))
        if command[:3] == ["docker", "image", "inspect"]:
            stdout = env if "--format" in command else ""
            return subprocess.CompletedProcess(command, 0 if present else 1, stdout, "")
        if command[:3] == ["docker", "buildx", "build"] and "--output" in command:
            destination = Path(command[command.index("--output") + 1].split("dest=", 1)[1])
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "results.tsv").write_text("rule_a\tpass\n", encoding="utf-8")
            (destination / "blocking.tsv").write_text("rule_b\tfail\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    return commands, run


def test_an_absent_base_is_scanned_before_it_is_built(tmp_path) -> None:
    commands, run = _recorder(present=False)
    base = al2023_base.Base("arm64", run)

    reference = base.reference(REPO_ROOT)

    builds = [command for command in commands if command[:3] == ["docker", "buildx", "build"]]
    assert builds[0][builds[0].index("--target") + 1] == "evidence"
    assert builds[1][builds[1].index("--target") + 1] == "base"
    assert reference.startswith("agentic-dev-platform-al2023-base:")
    assert base.reference(REPO_ROOT) == reference
    assert len([c for c in commands if c[:3] == ["docker", "buildx", "build"]]) == 2


def test_a_present_base_is_reused_without_a_build() -> None:
    commands, run = _recorder(present=True)

    al2023_base.Base("arm64", run).reference(REPO_ROOT)

    assert not any(command[:3] == ["docker", "buildx", "build"] for command in commands)


def test_only_a_dockerfile_that_starts_from_the_base_receives_it() -> None:
    _, run = _recorder(present=True)
    base = al2023_base.Base("arm64", run)

    assert set(base.build_args(REPO_ROOT, "FROM ${AL2023_IMAGE} AS go\n")) == {"AL2023_IMAGE"}
    assert base.build_args(REPO_ROOT, "FROM scratch\n") == {}


def test_a_scan_reports_the_blocking_rules_and_writes_the_evidence(tmp_path) -> None:
    _, run = _recorder(present=True, env='["PATH=/usr/bin::/bin"]')
    base = al2023_base.Base("arm64", run, evidence=tmp_path)

    problems = base.scan(REPO_ROOT, "registry/image:tag", "image")

    assert problems == [
        "registry/image:tag: CIS rule rule_b fail",
        "registry/image:tag: PATH entry '' is empty",
    ]
    assert (tmp_path / "image" / "results.tsv").is_file()


class _Ecr:
    class exceptions:
        class ImageNotFoundException(Exception):
            pass

    def __init__(self, pushed: set[str]) -> None:
        self.pushed = pushed

    def describe_images(self, repositoryName: str, imageIds: list[dict[str, str]]):
        if repositoryName not in self.pushed:
            raise self.exceptions.ImageNotFoundException
        return {"imageDetails": [{"imageDigest": "sha256:" + "a" * 64}]}


def test_share_pushes_the_base_and_scanner_a_repository_lacks() -> None:
    commands, run = _recorder(present=True)
    registry = "111111111111.dkr.ecr.us-east-1.amazonaws.com"

    al2023_base.Base("arm64", run).share(
        REPO_ROOT, _Ecr({"agentic-platform/al2023-base"}), registry, "agentic-platform"
    )

    pushes = [command[2] for command in commands if command[:2] == ["docker", "push"]]
    assert len(pushes) == 1
    assert pushes[0].startswith(f"{registry}/agentic-platform/al2023-openscap:")


def test_seed_pulls_the_shared_images_under_their_local_names() -> None:
    commands, run = _recorder(present=True)
    registry = "111111111111.dkr.ecr.us-east-1.amazonaws.com"

    al2023_base.Base("arm64", run).seed(REPO_ROOT, registry, "agentic-platform")

    tags = [command[2:] for command in commands if command[:2] == ["docker", "tag"]]
    assert [local.split(":")[0] for _, local in tags] == [
        "agentic-dev-platform-al2023-base",
        "agentic-dev-platform-al2023-openscap",
    ]
    assert all(shared.split(":")[1] == local.split(":")[1] for shared, local in tags)


def test_a_group_job_does_not_scan_a_seeded_base_again(tmp_path) -> None:
    commands, run = _recorder(present=True)

    al2023_base.Base("arm64", run, evidence=tmp_path, record_base=False).reference(REPO_ROOT)

    assert not any(command[:3] == ["docker", "buildx", "build"] for command in commands)
