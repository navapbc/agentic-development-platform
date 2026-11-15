"""Every platform Dockerfile builds on the pins in docker/al2023/versions.env."""

from __future__ import annotations

from pathlib import Path

import pytest
from scripts import base_versions


def test_every_dockerfile_pin_matches_the_versions_file() -> None:
    assert base_versions.drift(base_versions.load(), base_versions.dockerfiles()) == []


def test_update_rewrites_only_the_drifted_pins(tmp_path: Path) -> None:
    dockerfile = tmp_path / "x.Dockerfile"
    dockerfile.write_text(
        "ARG AL2023_RELEASE=2023.1.1\nFROM scratch\nARG GO_VERSION=1.0\nARG OTHER=keep\n",
        encoding="utf-8",
    )
    pins = {"AL2023_RELEASE": "2023.12.20261112", "GO_VERSION": "1.27.4"}

    assert base_versions.drift(pins, [dockerfile]) == [
        "x.Dockerfile line 1 sets AL2023_RELEASE=2023.1.1, not 2023.12.20261112",
        "x.Dockerfile line 3 sets GO_VERSION=1.0, not 1.27.4",
    ]
    assert base_versions.update(pins, [dockerfile]) == [dockerfile]
    assert dockerfile.read_text(encoding="utf-8") == (
        "ARG AL2023_RELEASE=2023.12.20261112\nFROM scratch\nARG GO_VERSION=1.27.4\nARG OTHER=keep\n"
    )
    assert base_versions.drift(pins, [dockerfile]) == []


def test_a_malformed_versions_line_is_refused(tmp_path: Path) -> None:
    versions = tmp_path / "versions.env"
    versions.write_text("GO_VERSION=1.27.4\nnot a pin\n", encoding="utf-8")

    with pytest.raises(ValueError, match="line 2 is not NAME=value"):
        base_versions.load(versions)


def test_update_keeps_the_blank_lines_around_a_pin(tmp_path: Path) -> None:
    dockerfile = tmp_path / "x.Dockerfile"
    dockerfile.write_text("ARG GO_VERSION=1.0\n\nFROM scratch\n", encoding="utf-8")

    base_versions.update({"GO_VERSION": "1.27.4"}, [dockerfile])

    assert dockerfile.read_text(encoding="utf-8") == "ARG GO_VERSION=1.27.4\n\nFROM scratch\n"
