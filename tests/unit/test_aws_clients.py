"""Tests for the S3 client builder shared by the evidence store paths."""

from __future__ import annotations

import pytest
from scripts.aws_clients import s3_client


@pytest.fixture
def missing_profile(monkeypatch, tmp_path):
    monkeypatch.setenv("AWS_PROFILE", "profile-that-is-not-defined")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "credentials"))


def test_explicit_keys_ignore_an_undefined_aws_profile(missing_profile):
    client = s3_client(
        region="us-east-1",
        endpoint_url="http://minio:9000",
        access_key="minio",
        secret_key="change-me-locally",
    )

    credentials = client._get_credentials()
    assert credentials is not None
    assert credentials.access_key == "minio"
    assert client.meta.endpoint_url == "http://minio:9000"
    assert client.meta.region_name == "us-east-1"


def test_without_keys_the_default_chain_reads_aws_profile(missing_profile):
    from botocore.exceptions import ProfileNotFound

    with pytest.raises(ProfileNotFound):
        s3_client(region="us-east-1")


def test_one_key_alone_uses_the_default_chain(missing_profile):
    from botocore.exceptions import ProfileNotFound

    with pytest.raises(ProfileNotFound):
        s3_client(region="us-east-1", access_key="minio")
