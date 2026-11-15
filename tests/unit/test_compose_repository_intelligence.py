"""The repository intelligence services receive only the settings and config they read."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from scripts.repositories_compose import READERS

REPO_ROOT = Path(__file__).resolve().parents[2]


def _services() -> dict[str, Any]:
    return yaml.safe_load((REPO_ROOT / "compose.yaml").read_text(encoding="utf-8"))["services"]


def _sources(service: dict[str, Any]) -> list[str]:
    return [
        volume.split(":", 1)[0] if isinstance(volume, str) else volume.get("source", "")
        for volume in service.get("volumes") or []
    ]


@pytest.mark.parametrize("name", READERS)
def test_the_service_reads_no_env_file_and_mounts_no_config_directory(name: str) -> None:
    service = _services()[name]

    assert "env_file" not in service
    assert [source for source in _sources(service) if source.startswith("./config")] == []


@pytest.mark.parametrize("name", READERS)
def test_the_service_declares_the_tenant_settings_from_the_compose_environment(name: str) -> None:
    environment = _services()[name]["environment"]

    assert environment["TENANTS"] == "${TENANTS:-}"
    assert environment["TENANT"] == "${TENANT:-}"
    assert environment["ENVIRONMENT"] == "${ENVIRONMENT:-local}"


def test_only_repository_intelligence_receives_the_token_key() -> None:
    services = _services()

    assert "REPO_INTELLIGENCE_TOKEN_KEY" in services["repo-intelligence"]["environment"]
    assert "REPO_INTELLIGENCE_TOKEN_KEY" not in services["repo-index-worker"]["environment"]
