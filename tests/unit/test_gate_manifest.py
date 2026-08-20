"""Unit tests for the gate determinism-manifest inputs."""

from __future__ import annotations

from services.orchestration.gates.config import parse_gates_config
from services.orchestration.gates.contract import GATE_CONTRACT_VERSION
from services.orchestration.gates.manifest import (
    gate_config_hash,
    gate_manifest,
    gate_versions,
)


def _config(raw):
    return parse_gates_config(raw)


def test_versions_cover_the_enabled_builtin_gates():
    config = _config({"profile": "government"})

    versions = gate_versions(config)

    assert versions == {
        "code-review": GATE_CONTRACT_VERSION,
        "security-review": GATE_CONTRACT_VERSION,
        "acceptance": GATE_CONTRACT_VERSION,
        "semgrep": GATE_CONTRACT_VERSION,
        "trivy": GATE_CONTRACT_VERSION,
    }


def test_disabled_gate_drops_from_the_versions():
    config = _config(
        {"profile": "government", "gates": [{"name": "semgrep", "enabled": False}]}
    )

    assert "semgrep" not in gate_versions(config)


def test_config_hash_is_stable_and_content_addressed():
    config = _config({"profile": "government"})

    first = gate_config_hash(config)
    second = gate_config_hash(config)

    assert first == second
    assert first.startswith("sha256:")


def test_config_hash_changes_when_the_gate_set_changes():
    government = gate_config_hash(_config({"profile": "government"}))
    private = gate_config_hash(_config({"profile": "private"}))

    assert government != private


def test_manifest_bundles_versions_and_hash():
    config = _config({"profile": "private"})

    manifest = gate_manifest(config)

    assert manifest.config_hash == gate_config_hash(config)
    assert manifest.versions == gate_versions(config)
