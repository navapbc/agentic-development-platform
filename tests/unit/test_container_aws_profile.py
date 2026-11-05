"""The platform containers must not inherit a host AWS profile they cannot resolve."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTAINER_PROFILE = "${PLATFORM_AWS_PROFILE:-${AWS_PROFILE}}"


def _tracked_compose_files() -> list[Path]:
    """The platform compose files in the order `tasks.py` passes them to compose."""
    listing = subprocess.run(
        ["git", "ls-files", "compose*.yaml"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    applied_first = ["compose.yaml"]
    ordered = [name for name in applied_first if name in listing]
    ordered += sorted(name for name in listing if name not in applied_first)
    return [REPO_ROOT / name for name in ordered]


def _services(compose_file: Path) -> dict[str, Any]:
    document = yaml.safe_load(compose_file.read_text(encoding="utf-8")) or {}
    return document.get("services") or {}


def _environment(service: dict[str, Any]) -> dict[str, str]:
    declared = service.get("environment") or {}
    if isinstance(declared, dict):
        return {key: str(value) for key, value in declared.items()}
    pairs = [entry.split("=", 1) for entry in declared]
    return {pair[0]: pair[1] if len(pair) == 2 else "" for pair in pairs}


def _merged_services() -> dict[str, dict[str, Any]]:
    """Every service as compose resolves it, with later files layered over earlier ones."""
    merged: dict[str, dict[str, Any]] = {}
    for compose_file in _tracked_compose_files():
        for name, service in _services(compose_file).items():
            target = merged.setdefault(name, {"volumes": [], "environment": {}})
            target["volumes"].extend(service.get("volumes") or [])
            target["environment"].update(_environment(service))
    return merged


def _mounts_aws_credentials(service: dict[str, Any]) -> bool:
    for volume in service["volumes"]:
        source = volume.split(":")[0] if isinstance(volume, str) else volume.get("source", "")
        if source.rstrip("/").endswith(".aws"):
            return True
    return False


def test_a_tracked_compose_file_mounts_the_aws_directory():
    services = _merged_services()

    assert [name for name, service in services.items() if _mounts_aws_credentials(service)]


def test_every_service_mounting_aws_credentials_resolves_its_profile_through_the_override():
    services = _merged_services()

    for name, service in services.items():
        if not _mounts_aws_credentials(service):
            continue
        assert service["environment"].get("AWS_PROFILE") == CONTAINER_PROFILE, (
            f"{name} mounts AWS credentials without routing AWS_PROFILE "
            "through PLATFORM_AWS_PROFILE"
        )


@pytest.mark.parametrize("compose_file", _tracked_compose_files(), ids=lambda path: path.name)
def test_no_compose_file_passes_the_host_profile_straight_to_a_container(compose_file: Path):
    for name, service in _services(compose_file).items():
        declared = _environment(service).get("AWS_PROFILE")
        assert declared in (None, CONTAINER_PROFILE), f"{name} passes the host profile through"


def test_the_env_example_declares_the_container_profile_override():
    env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")

    assert "\nPLATFORM_AWS_PROFILE=" in env_example
