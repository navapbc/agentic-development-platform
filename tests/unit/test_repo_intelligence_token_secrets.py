"""Unit tests for the source of the tokens repository intelligence accepts from each tenant."""

from __future__ import annotations

import json
import logging

import boto3
import pytest
from botocore.stub import Stubber
from services.integration.credentials import ConfigError
from services.repository_intelligence.tenant_tokens import derive_token, tenant_for_token
from services.repository_intelligence.token_secrets import (
    ARN_MAP_ENV,
    SecretTokens,
    StaticTokens,
    secret_arns,
    token_source,
)
from tests.support import hermetic_settings

SECRETS = "arn:aws:secretsmanager:us-east-1:111111111111:secret:agentic-platform/tenants"
ADP_ARN = f"{SECRETS}/adp/repo-intelligence-token-a1b2c3"
BETA_ARN = f"{SECRETS}/beta/repo-intelligence-token-d4e5f6"


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def client():
    secrets_client = boto3.client(
        "secretsmanager",
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",  # noqa: S106
    )
    with Stubber(secrets_client) as stubber:
        yield secrets_client, stubber
        stubber.assert_no_pending_responses()


def _value(stubber: Stubber, arn: str, stage: str, value: str) -> None:
    stubber.add_response(
        "get_secret_value",
        {"ARN": arn, "Name": arn.rsplit(":", 1)[-1], "SecretString": value},
        {"SecretId": arn, "VersionStage": stage},
    )


def _error(stubber: Stubber, arn: str, stage: str, code: str) -> None:
    stubber.add_client_error(
        "get_secret_value",
        service_error_code=code,
        expected_params={"SecretId": arn, "VersionStage": stage},
    )


def test_secret_arns_read_the_tenant_map_and_refuse_a_malformed_one():
    assert secret_arns({}) == {}
    assert secret_arns({ARN_MAP_ENV: json.dumps({"adp": f" {ADP_ARN} "})}) == {"adp": ADP_ARN}

    for malformed in ("{", '["adp"]', '{"adp": ""}', '{"adp": 1}'):
        with pytest.raises(ConfigError):
            secret_arns({ARN_MAP_ENV: malformed})


def test_the_current_and_the_previous_token_of_each_tenant_are_accepted(client):
    secrets_client, stubber = client
    _value(stubber, ADP_ARN, "AWSCURRENT", "adp-new")
    _value(stubber, ADP_ARN, "AWSPREVIOUS", "adp-old")
    _value(stubber, BETA_ARN, "AWSCURRENT", "beta-1")
    _error(stubber, BETA_ARN, "AWSPREVIOUS", "ResourceNotFoundException")

    tokens = SecretTokens(secrets_client, {"adp": ADP_ARN, "beta": BETA_ARN}, interval=60)

    accepted = tokens.accepted()
    assert accepted == {"adp": ("adp-new", "adp-old"), "beta": ("beta-1",)}
    assert tenant_for_token(accepted, "adp-old") == "adp"
    assert tenant_for_token(accepted, "beta-1") == "beta"


def test_a_previous_version_equal_to_the_current_one_is_accepted_once(client):
    secrets_client, stubber = client
    _value(stubber, ADP_ARN, "AWSCURRENT", "adp-1")
    _value(stubber, ADP_ARN, "AWSPREVIOUS", "adp-1")

    assert SecretTokens(secrets_client, {"adp": ADP_ARN}, interval=60).accepted() == {
        "adp": ("adp-1",)
    }


def test_the_tokens_are_read_again_once_the_refresh_interval_has_passed(client):
    secrets_client, stubber = client
    clock = _Clock()
    tokens = SecretTokens(secrets_client, {"adp": ADP_ARN}, interval=60, clock=clock)
    _value(stubber, ADP_ARN, "AWSCURRENT", "adp-1")
    _error(stubber, ADP_ARN, "AWSPREVIOUS", "ResourceNotFoundException")
    assert tokens.accepted() == {"adp": ("adp-1",)}

    clock.now = 59
    assert tokens.accepted() == {"adp": ("adp-1",)}

    clock.now = 60
    _value(stubber, ADP_ARN, "AWSCURRENT", "adp-2")
    _value(stubber, ADP_ARN, "AWSPREVIOUS", "adp-1")
    assert tokens.accepted() == {"adp": ("adp-2", "adp-1")}


def test_a_miss_reads_the_tokens_again_at_most_once_per_miss_interval(client):
    secrets_client, stubber = client
    clock = _Clock()
    tokens = SecretTokens(
        secrets_client, {"adp": ADP_ARN}, interval=300, miss_interval=10, clock=clock
    )
    _value(stubber, ADP_ARN, "AWSCURRENT", "adp-1")
    _error(stubber, ADP_ARN, "AWSPREVIOUS", "ResourceNotFoundException")
    tokens.accepted()

    clock.now = 5
    assert tokens.reload_after_miss() is None

    clock.now = 10
    _value(stubber, ADP_ARN, "AWSCURRENT", "adp-2")
    _value(stubber, ADP_ARN, "AWSPREVIOUS", "adp-1")
    assert tokens.reload_after_miss() == {"adp": ("adp-2", "adp-1")}
    assert tokens.reload_after_miss() is None


def test_an_unreadable_secret_keeps_the_last_accepted_tokens(client, caplog):
    secrets_client, stubber = client
    clock = _Clock()
    tokens = SecretTokens(secrets_client, {"adp": ADP_ARN}, interval=60, clock=clock)
    _value(stubber, ADP_ARN, "AWSCURRENT", "adp-2")
    _value(stubber, ADP_ARN, "AWSPREVIOUS", "adp-1")
    tokens.accepted()

    clock.now = 60
    _error(stubber, ADP_ARN, "AWSCURRENT", "AccessDeniedException")
    with caplog.at_level(logging.ERROR):
        assert tokens.accepted() == {"adp": ("adp-2", "adp-1")}
    assert "keeping its last accepted values" in caplog.text


def test_an_unreadable_previous_version_keeps_the_last_tokens_beside_the_current_one(client):
    secrets_client, stubber = client
    clock = _Clock()
    tokens = SecretTokens(secrets_client, {"adp": ADP_ARN}, interval=60, clock=clock)
    _value(stubber, ADP_ARN, "AWSCURRENT", "adp-2")
    _value(stubber, ADP_ARN, "AWSPREVIOUS", "adp-1")
    tokens.accepted()

    clock.now = 60
    _value(stubber, ADP_ARN, "AWSCURRENT", "adp-3")
    _error(stubber, ADP_ARN, "AWSPREVIOUS", "ThrottlingException")
    assert tokens.accepted() == {"adp": ("adp-3", "adp-2", "adp-1")}


def test_a_secret_without_a_current_value_accepts_no_token(client):
    secrets_client, stubber = client
    _value(stubber, ADP_ARN, "AWSCURRENT", "  ")

    assert SecretTokens(secrets_client, {"adp": ADP_ARN}, interval=60).accepted() == {}


def test_without_secret_arns_the_source_holds_the_derived_tokens():
    settings = hermetic_settings(tenants="adp,beta", repo_intelligence_token_key="k")

    def refuse(_settings):
        raise AssertionError("no Secrets Manager client is built without secret ARNs")

    source = token_source(settings, environ={}, client_factory=refuse)

    assert isinstance(source, StaticTokens)
    assert source.accepted() == {
        "adp": (derive_token("k", "adp"),),
        "beta": (derive_token("k", "beta"),),
    }
    assert source.reload_after_miss() is None


def test_secret_arns_select_secrets_manager_for_the_served_tenants(client, caplog):
    secrets_client, stubber = client
    settings = hermetic_settings(
        tenants="adp,beta", repo_intelligence_token_key="k", tenant_config_refresh_seconds=30
    )
    environ = {ARN_MAP_ENV: json.dumps({"adp": ADP_ARN, "other": BETA_ARN})}

    with caplog.at_level(logging.ERROR):
        source = token_source(settings, environ=environ, client_factory=lambda _s: secrets_client)
    _value(stubber, ADP_ARN, "AWSCURRENT", "adp-1")
    _error(stubber, ADP_ARN, "AWSPREVIOUS", "ResourceNotFoundException")

    assert isinstance(source, SecretTokens)
    assert source.accepted() == {"adp": ("adp-1",)}
    assert "maps no ARN for tenants beta" in caplog.text
