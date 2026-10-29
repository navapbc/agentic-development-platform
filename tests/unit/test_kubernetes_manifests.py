"""The sandbox overlay must clear the synthetic gateway host list the base manifest carries."""

from __future__ import annotations

import shutil
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest
import yaml
from services import process

MANIFESTS = Path(__file__).resolve().parents[2] / "deploy" / "kubernetes"
LOCAL_OVERLAY = MANIFESTS / "local"
RENDER = ["kubectl", "kustomize", "--load-restrictor", "LoadRestrictionsNone", str(LOCAL_OVERLAY)]


def _container_env(documents: Iterable[dict[str, Any] | None], name: str) -> dict[str, Any]:
    for document in documents:
        if not document or document.get("kind") != "Deployment":
            continue
        if document["metadata"]["name"] != name:
            continue
        containers = document["spec"]["template"]["spec"]["containers"]
        container = next(entry for entry in containers if entry["name"] == name)
        return {entry["name"]: entry.get("value") for entry in container.get("env", [])}
    raise AssertionError(f"no Deployment named {name}")


def _overlay_patch_env(kind: str, name: str) -> dict[str, Any]:
    overlay = yaml.safe_load((LOCAL_OVERLAY / "kustomization.yaml").read_text())
    for patch in overlay["patches"]:
        target = patch.get("target") or {}
        if target.get("kind") == kind and target.get("name") == name:
            document = yaml.safe_load(patch["patch"])
            if document.get("$patch") == "delete":
                continue
            return _container_env([document], name)
    raise AssertionError(f"no {kind} patch named {name} in the local overlay")


def test_local_overlay_clears_the_synthetic_gateway_allowed_hosts():
    base = _container_env(
        yaml.safe_load_all((MANIFESTS / "mcp-gateway.yaml").read_text()), "mcp-gateway"
    )

    assert base["MCP_ALLOWED_HOSTS"]
    assert _overlay_patch_env("Deployment", "mcp-gateway")["MCP_ALLOWED_HOSTS"] == ""


@pytest.mark.skipif(shutil.which("kubectl") is None, reason="kubectl renders the overlay")
def test_rendered_local_overlay_leaves_the_gateway_host_header_unchecked():
    rendered = process.run(
        RENDER,
        timeout=process.SHORT_COMMAND_TIMEOUT_SECONDS,
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    assert _container_env(yaml.safe_load_all(rendered), "mcp-gateway")["MCP_ALLOWED_HOSTS"] == ""
