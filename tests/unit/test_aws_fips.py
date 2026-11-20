"""Tests for the AWS security baseline hardening.

The FIPS bridge exports the botocore ``AWS_USE_FIPS_ENDPOINT`` variable from the platform
setting, and botocore then resolves FIPS service endpoints. The resolution is verified
offline against real botocore endpoint resolution, and the compose wiring is asserted so
every AWS-SDK service receives the variable.
"""

from __future__ import annotations

from pathlib import Path

import boto3
import pytest
import yaml
from scripts.aws_clients import configure_aws_fips
from tests.support import hermetic_settings

_ROOT = Path(__file__).resolve().parents[2]
_FIPS = "AWS_USE_FIPS_ENDPOINT"


def test_bridge_exports_env_when_enabled(monkeypatch):
    monkeypatch.delenv(_FIPS, raising=False)
    configure_aws_fips(hermetic_settings(aws_use_fips_endpoint=True))
    import os

    assert os.environ[_FIPS] == "true"


def test_bridge_is_noop_when_disabled(monkeypatch):
    monkeypatch.delenv(_FIPS, raising=False)
    configure_aws_fips(hermetic_settings(aws_use_fips_endpoint=False))
    import os

    assert _FIPS not in os.environ


def test_bridge_does_not_override_existing(monkeypatch):
    monkeypatch.setenv(_FIPS, "false")
    configure_aws_fips(hermetic_settings(aws_use_fips_endpoint=True))
    import os

    assert os.environ[_FIPS] == "false"


def test_s3_client_resolves_a_fips_endpoint(monkeypatch):
    monkeypatch.setenv(_FIPS, "true")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "y")
    client = boto3.client("s3", region_name="us-east-1")
    assert "fips" in client.meta.endpoint_url


def test_bedrock_client_resolves_a_fips_endpoint(monkeypatch):
    monkeypatch.setenv(_FIPS, "true")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "y")
    client = boto3.client("bedrock-runtime", region_name="us-east-1")
    assert "fips" in client.meta.endpoint_url


def test_compose_wires_fips_into_the_workflow_worker():
    compose = yaml.safe_load((_ROOT / "compose.yaml").read_text(encoding="utf-8"))
    assert _FIPS in compose["services"]["workflow-worker"]["environment"]


def _container(manifest: str, name: str) -> dict:
    documents = yaml.safe_load_all((_ROOT / "deploy" / "kubernetes" / manifest).read_text())
    deployment = next(d for d in documents if d and d["kind"] == "Deployment")
    return next(
        c for c in deployment["spec"]["template"]["spec"]["containers"] if c["name"] == name
    )


def test_litellm_in_the_cluster_reads_the_fips_setting():
    names = [variable["name"] for variable in _container("litellm.yaml", "litellm")["env"]]
    assert _FIPS in names


@pytest.mark.parametrize("worker", ["activity-worker", "review-worker"])
def test_the_cluster_workers_read_every_platform_setting_including_fips(worker):
    container = _container(f"{worker}.yaml", worker)
    assert {"secretRef": {"name": "agentic-platform-env"}} in container["envFrom"]
