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
            }
            for spec in config.gates
        ],
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(blob.encode('utf-8')).hexdigest()}"


def gate_versions(config: GatesConfig) -> dict[str, str]:
    return {spec.name: GATE_CONTRACT_VERSION for spec in config.enabled_gates()}


def gate_manifest(config: GatesConfig) -> GateManifest:
    return GateManifest(versions=gate_versions(config), config_hash=gate_config_hash(config))
