from __future__ import annotations

from pathlib import Path

import pytest
from services.integration.credentials import (
    ConfigError,
    config_status,
    current_config_version,
    require_current_config,
)


def _write(root: Path, name: str, suffix: str, body: str) -> None:
    directory = root / "config" / "local"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.yaml{suffix}").write_text(body, encoding="utf-8")


def test_current_config_version_reads_the_code_constant() -> None:
    assert current_config_version("github") == 1
    assert current_config_version("jira") == 1
    assert current_config_version("integrations") == 1


def test_unregistered_name_has_no_current_version() -> None:
    with pytest.raises(ConfigError, match="no current config version"):
        current_config_version("widgets")


def test_up_to_date_passes(tmp_path: Path) -> None:
    _write(tmp_path, "widgets", ".example", "config_version: 1\nmodes: {}\n")
    _write(tmp_path, "widgets", "", "config_version: 1\nmodes: {}\n")

    status = config_status("widgets", environment="local", root=tmp_path, current=1)

    assert status.present is True
    assert status.up_to_date is True
    assert status.operator_version == 1
    assert status.current_version == 1
    assert status.example_matches_current is True
    require_current_config("widgets", environment="local", root=tmp_path, current=1)


def test_require_current_config_does_not_read_the_example(tmp_path: Path) -> None:
    _write(tmp_path, "widgets", "", "config_version: 1\nmodes: {}\n")

    require_current_config("widgets", environment="local", root=tmp_path, current=1)


def test_outdated_fails_closed(tmp_path: Path) -> None:
    _write(tmp_path, "widgets", "", "config_version: 1\nmodes: {}\n")

    with pytest.raises(ConfigError, match="expects version 2"):
        require_current_config("widgets", environment="local", root=tmp_path, current=2)


def test_config_status_reports_template_drift(tmp_path: Path) -> None:
    _write(tmp_path, "widgets", ".example", "config_version: 1\nmodes: {}\n")
    _write(tmp_path, "widgets", "", "config_version: 1\nmodes: {}\n")

    status = config_status("widgets", environment="local", root=tmp_path, current=2)

    assert status.example_version == 1
    assert status.current_version == 2
    assert status.example_matches_current is False


def test_missing_operator_file_does_not_fail_closed(tmp_path: Path) -> None:
    require_current_config("widgets", environment="local", root=tmp_path, current=1)


def test_config_status_requires_the_example(tmp_path: Path) -> None:
    _write(tmp_path, "widgets", "", "config_version: 1\n")

    with pytest.raises(ConfigError, match="missing committed template"):
        config_status("widgets", environment="local", root=tmp_path, current=1)


def test_non_integer_version_raises(tmp_path: Path) -> None:
    _write(tmp_path, "widgets", "", "config_version: nope\n")

    with pytest.raises(ConfigError, match="config_version must be an integer"):
        require_current_config("widgets", environment="local", root=tmp_path, current=1)
