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


@pytest.mark.parametrize(
    "service", ["workflow-worker", "activity-worker", "review-worker", "litellm"]
)
def test_compose_wires_fips_into_every_aws_sdk_service(service):
    compose = yaml.safe_load((_ROOT / "compose.yaml").read_text(encoding="utf-8"))
    env = compose["services"][service]["environment"]
    assert _FIPS in env
