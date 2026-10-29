"""Build identifier derivation, its ordering, and where the image carries it."""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

import yaml
from scripts.build_id import (
    IMAGE_SOURCE_PATHS,
    LEDGER_NAME,
    generate_build_id,
    recorded_build_id,
    release_version,
    resolve_build_id,
    source_digest,
)
from services.orchestration.suspension import is_newer_build

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = REPO_ROOT / "compose.yaml"
ENV_EXAMPLE_PATH = REPO_ROOT / ".env.example"
DOCKERFILE_PATH = REPO_ROOT / "docker" / "Dockerfile"


def test_a_later_build_of_one_release_outranks_an_earlier_one():
    earlier = generate_build_id(datetime(2026, 10, 28, 9, 30, 0, tzinfo=UTC))
    later = generate_build_id(datetime(2026, 10, 28, 9, 30, 1, tzinfo=UTC))

    assert is_newer_build(later, earlier) is True
    assert is_newer_build(earlier, later) is False
    assert is_newer_build(earlier, earlier) is False


def test_a_stamped_build_outranks_the_unstamped_release_floor():
    stamped = generate_build_id(datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC))

    assert is_newer_build(stamped, release_version()) is True
    assert is_newer_build(release_version(), stamped) is False


def test_a_release_bump_outranks_an_older_release_carrying_a_later_stamp():
    assert is_newer_build("0.4.0.20260101000000", "0.3.0.20261231235959") is True
    assert is_newer_build("0.10.0.20260101000000", "0.9.0.20261231235959") is True


def test_the_identifier_parses_as_release_parts_followed_by_the_stamp():
    build_id = generate_build_id(datetime(2026, 10, 28, 9, 30, 0, tzinfo=UTC))

    assert build_id == f"{release_version()}.20261028093000"
    assert tuple(int(part) for part in re.findall(r"\d+", build_id))[-1] == 20261028093000


def _source_tree(root: Path) -> None:
    (root / "services").mkdir()
    (root / "services" / "app.py").write_text("print('one')\n")
    (root / "docker").mkdir()
    (root / "docker" / "Dockerfile").write_text("FROM scratch\n")
    (root / "pyproject.toml").write_text("[project]\nname = 'x'\n")


def test_an_unchanged_source_tree_reuses_the_recorded_identifier(tmp_path):
    _source_tree(tmp_path)

    first = resolve_build_id(tmp_path, datetime(2026, 10, 28, 9, 30, 0, tzinfo=UTC))
    second = resolve_build_id(tmp_path, datetime(2026, 10, 29, 11, 0, 0, tzinfo=UTC))

    assert first == second
    assert json.loads((tmp_path / LEDGER_NAME).read_text())["build_id"] == first


def test_a_changed_source_tree_mints_a_newer_identifier(tmp_path):
    _source_tree(tmp_path)
    first = resolve_build_id(tmp_path, datetime(2026, 10, 28, 9, 30, 0, tzinfo=UTC))

    (tmp_path / "services" / "app.py").write_text("print('two')\n")
    second = resolve_build_id(tmp_path, datetime(2026, 10, 29, 11, 0, 0, tzinfo=UTC))

    assert second != first
    assert is_newer_build(second, first) is True


def test_a_byte_compiled_artifact_does_not_change_the_identifier(tmp_path):
    _source_tree(tmp_path)
    first = resolve_build_id(tmp_path, datetime(2026, 10, 28, 9, 30, 0, tzinfo=UTC))

    cache = tmp_path / "services" / "__pycache__"
    cache.mkdir()
    (cache / "app.cpython-312.pyc").write_bytes(b"\x00\x01")

    assert resolve_build_id(tmp_path, datetime(2026, 10, 29, 11, 0, 0, tzinfo=UTC)) == first


def test_a_file_outside_the_image_sources_does_not_change_the_digest(tmp_path):
    _source_tree(tmp_path)
    before = source_digest(tmp_path)

    (tmp_path / "notes.txt").write_text("scratch\n")

    assert source_digest(tmp_path) == before


def test_the_recorded_identifier_survives_a_source_change_after_the_build(tmp_path):
    _source_tree(tmp_path)
    built = resolve_build_id(tmp_path, datetime(2026, 10, 28, 9, 30, 0, tzinfo=UTC))

    (tmp_path / "services" / "app.py").write_text("print('edited since the build')\n")

    assert recorded_build_id(tmp_path) == built


def test_the_recorded_identifier_falls_back_to_the_release_version(tmp_path):
    assert recorded_build_id(tmp_path) == release_version()


def test_the_digest_covers_every_path_the_image_copies():
    copied = set()
    for line in DOCKERFILE_PATH.read_text().splitlines():
        if not line.startswith("COPY ") or "--from=" in line:
            continue
        copied.update(line.split()[1:-1])

    assert copied == set(IMAGE_SOURCE_PATHS) - {"docker/Dockerfile"}


def test_the_image_bakes_the_identifier_and_defaults_to_the_release_version():
    dockerfile = DOCKERFILE_PATH.read_text()
    compose = yaml.safe_load(COMPOSE_PATH.read_text())

    assert f"ARG AGENT_BUILD_ID={release_version()}" in dockerfile
    assert "ENV AGENT_BUILD_ID=${AGENT_BUILD_ID}" in dockerfile
    assert compose["services"]["workflow-worker"]["build"]["args"]["AGENT_BUILD_ID"] == (
        "${AGENT_BUILD_ID:-" + release_version() + "}"
    )


def test_no_worker_service_overrides_the_baked_identifier():
    compose = yaml.safe_load(COMPOSE_PATH.read_text())

    overriding = [
        name
        for name, service in compose["services"].items()
        if "AGENT_BUILD_ID" in (service.get("environment") or {})
    ]

    assert overriding == []
    assert "AGENT_BUILD_ID=" not in ENV_EXAMPLE_PATH.read_text()


def test_the_temporal_service_needs_no_versioning_dynamic_config():
    compose = yaml.safe_load(COMPOSE_PATH.read_text())
    temporal = compose["services"]["temporal"]

    assert "DYNAMIC_CONFIG_FILE_PATH" not in temporal["environment"]
    assert "volumes" not in temporal
    assert not (REPO_ROOT / "config" / "platform" / "temporal-dynamic-config.yaml").exists()


def test_local_up_passes_the_resolved_identifier_as_a_build_argument(monkeypatch):
    import tasks

    monkeypatch.setattr(tasks, "resolve_build_id", lambda: "0.3.0.20261028093000")
    monkeypatch.setattr(tasks, "db_migrate", lambda c: None)
    monkeypatch.delenv("AGENT_BUILD_ID", raising=False)

    from invoke import Context

    commands: list[str] = []
    monkeypatch.setattr(Context, "run", lambda self, command, **kwargs: commands.append(command))
    tasks.local_up(Context())

    build = next(command for command in commands if " build " in f"{command} ")
    assert "--build-arg AGENT_BUILD_ID=0.3.0.20261028093000" in build
    assert os.environ["AGENT_BUILD_ID"] == "0.3.0.20261028093000"
