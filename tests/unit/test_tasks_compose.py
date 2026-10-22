"""Compose argument construction for the invoke task runner."""

from __future__ import annotations

import pytest
from tasks import _compose_files, _env_file_value


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("COMPOSE_FILE", raising=False)
    return tmp_path


def test_compose_files_falls_back_to_the_base_file(project):
    assert _compose_files() == "-f compose.yaml"


def test_compose_files_adds_the_override_file_when_present(project):
    (project / "compose.override.yaml").write_text("services: {}\n", encoding="utf-8")

    assert _compose_files() == "-f compose.yaml -f compose.override.yaml"


def test_compose_files_defers_to_the_compose_file_environment_variable(project, monkeypatch):
    (project / "compose.override.yaml").write_text("services: {}\n", encoding="utf-8")
    monkeypatch.setenv("COMPOSE_FILE", "compose.yaml:compose.override.yaml:compose.pilot.yaml")

    assert _compose_files() == ""


def test_compose_files_reads_the_compose_file_variable_from_the_env_file(project):
    (project / "compose.override.yaml").write_text("services: {}\n", encoding="utf-8")
    (project / ".env").write_text(
        "# comment\nUNRELATED=1\nCOMPOSE_FILE=compose.yaml:compose.pilot.yaml\n", encoding="utf-8"
    )

    assert _compose_files() == ""


def test_compose_files_ignores_an_unrelated_env_file(project):
    (project / ".env").write_text("COMPOSE_PROJECT_NAME=platform\n", encoding="utf-8")

    assert _compose_files() == "-f compose.yaml"


def test_compose_files_treats_an_empty_env_file_value_as_unset(project):
    (project / ".env").write_text('COMPOSE_FILE=""\n', encoding="utf-8")

    assert _compose_files() == "-f compose.yaml"


@pytest.mark.parametrize(
    "line",
    [
        "COMPOSE_FILE=compose.yaml:compose.pilot.yaml",
        'COMPOSE_FILE="compose.yaml:compose.pilot.yaml"',
        "COMPOSE_FILE='compose.yaml:compose.pilot.yaml'",
        "COMPOSE_FILE = compose.yaml:compose.pilot.yaml",
    ],
)
def test_env_file_value_returns_the_unquoted_value(project, line):
    (project / ".env").write_text(f"{line}\n", encoding="utf-8")

    assert _env_file_value("COMPOSE_FILE") == "compose.yaml:compose.pilot.yaml"


def test_env_file_value_returns_empty_for_a_missing_key(project):
    (project / ".env").write_text("COMPOSE_PROJECT_NAME=platform\n", encoding="utf-8")

    assert _env_file_value("COMPOSE_FILE") == ""
