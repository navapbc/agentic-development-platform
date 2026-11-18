from __future__ import annotations

from pathlib import Path

import pytest
from services.integration.credentials import ConfigError
from services.integration.source_control import source_control_repos
from tests.support import configure_repositories, hermetic_settings


def test_every_configured_repository_is_reachable_whatever_its_backend(
    monkeypatch, tmp_path: Path
) -> None:
    configure_repositories(
        monkeypatch,
        tmp_path,
        "repositories:\n  svc: {}\n  notes:\n    backend: local\n    path: /srv/notes\n",
    )

    assert source_control_repos(hermetic_settings()) == {"svc", "notes"}


def test_a_local_repository_needs_no_github_config(monkeypatch, tmp_path: Path) -> None:
    configure_repositories(
        monkeypatch, tmp_path, "repositories:\n  notes:\n    backend: local\n    path: /srv/notes\n"
    )

    assert not (tmp_path / "github.yaml").exists()
    assert source_control_repos(hermetic_settings()) == {"notes"}


def test_an_environment_without_repositories_yaml_names_the_file(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PLATFORM_ENV_CONFIG_DIR", str(tmp_path))

    with pytest.raises(ConfigError, match=r"repositories\.yaml"):
        source_control_repos(hermetic_settings())
