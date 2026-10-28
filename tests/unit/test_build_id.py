"""Build identifier derivation and its ordering under the parked-build comparison."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import yaml
from scripts.build_id import generate_build_id, release_version
from services.orchestration.suspension import is_newer_build
from tasks import _stamp_build_id

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = REPO_ROOT / "compose.yaml"
ENV_EXAMPLE_PATH = REPO_ROOT / ".env.example"


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


def test_stamping_replaces_an_existing_entry_and_leaves_the_rest(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path(".env").write_text("LOG_LEVEL=INFO\nAGENT_BUILD_ID=0.3.0\nTEMPORAL_NAMESPACE=default\n")

    build_id = _stamp_build_id()

    lines = Path(".env").read_text().splitlines()
    assert lines.count(f"AGENT_BUILD_ID={build_id}") == 1
    assert len([line for line in lines if line.startswith("AGENT_BUILD_ID=")]) == 1
    assert "LOG_LEVEL=INFO" in lines
    assert "TEMPORAL_NAMESPACE=default" in lines
    assert is_newer_build(build_id, release_version()) is True


def test_stamping_creates_the_env_file_when_it_is_absent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    build_id = _stamp_build_id()

    assert Path(".env").read_text() == f"AGENT_BUILD_ID={build_id}\n"


def test_the_declared_fallbacks_track_the_release_version():
    compose = yaml.safe_load(COMPOSE_PATH.read_text())
    declared = {
        name: service["environment"]["AGENT_BUILD_ID"]
        for name, service in compose["services"].items()
        if "AGENT_BUILD_ID" in (service.get("environment") or {})
    }

    assert set(declared) == {"workflow-worker", "activity-worker", "review-worker"}
    assert set(declared.values()) == {"${AGENT_BUILD_ID:-" + release_version() + "}"}
    assert f"AGENT_BUILD_ID={release_version()}" in ENV_EXAMPLE_PATH.read_text().splitlines()
