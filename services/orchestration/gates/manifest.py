"""Determinism-manifest inputs derived from the resolved gate config."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from services.orchestration.gates.config import GatesConfig
from services.orchestration.gates.contract import GATE_CONTRACT_VERSION


@dataclass(frozen=True)
class GateManifest:
    versions: dict[str, str]
    config_hash: str


def gate_config_hash(config: GatesConfig) -> str:
    payload = {
        "profile": config.profile,
        "fail_fast": dict(sorted(config.fail_fast.items())),
        "gates": [
            {
                "name": spec.name,
                "enabled": spec.enabled,
                "required": spec.required,
                "after": list(spec.after),
                "on_block": spec.on_block,
                "params": spec.params,
                "distribution": spec.distribution,
                "entry_point": spec.entry_point,
            }
            for spec in config.gates
        ],
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(blob.encode('utf-8')).hexdigest()}"


def gate_versions(config: GatesConfig) -> dict[str, str]:
    from importlib.metadata import PackageNotFoundError, version

    versions: dict[str, str] = {}
    for spec in config.enabled_gates():
        if spec.is_plugin:
            try:
                versions[spec.name] = version(spec.distribution)
            except PackageNotFoundError:
                versions[spec.name] = "unknown"
        else:
            versions[spec.name] = GATE_CONTRACT_VERSION
    return versions


def gate_manifest(config: GatesConfig) -> GateManifest:
    return GateManifest(versions=gate_versions(config), config_hash=gate_config_hash(config))
