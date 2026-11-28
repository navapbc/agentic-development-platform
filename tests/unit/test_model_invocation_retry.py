"""How the platform's direct gateway call classifies and retries a failed Claude request."""

from __future__ import annotations

import httpx
import pytest
from services.orchestration.temporal_activities.bedrock import (
    _RETRYABLE_STATUS,
    MODEL_RETRY_POLICY,
    _raise_for_status,
)
from temporalio.exceptions import ApplicationError


def _response(status_code: int, text: str = "") -> httpx.Response:
    return httpx.Response(status_code, text=text)


def test_a_successful_response_raises_nothing():
    assert _raise_for_status(_response(200)) is None


@pytest.mark.parametrize("status", sorted(_RETRYABLE_STATUS))
def test_a_transient_status_raises_a_retryable_error(status: int):
    with pytest.raises(ApplicationError) as caught:
        _raise_for_status(_response(status, "throttled"))

    assert caught.value.non_retryable is False
    assert str(status) in str(caught.value)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_a_rejected_request_raises_a_non_retryable_error(status: int):
    with pytest.raises(ApplicationError) as caught:
        _raise_for_status(_response(status, "bad request"))

    assert caught.value.non_retryable is True


def test_the_error_detail_is_truncated():
    with pytest.raises(ApplicationError) as caught:
        _raise_for_status(_response(500, "x" * 5000))

    assert len(str(caught.value)) < 600


def test_the_retry_policy_backs_off_exponentially_across_several_attempts():
    assert MODEL_RETRY_POLICY.maximum_attempts >= 5
    assert MODEL_RETRY_POLICY.backoff_coefficient > 1.0
    assert MODEL_RETRY_POLICY.initial_interval is not None
    assert MODEL_RETRY_POLICY.maximum_interval is not None
    assert MODEL_RETRY_POLICY.maximum_interval > MODEL_RETRY_POLICY.initial_interval


_EXPIRED_SESSION = (
    '{"error":{"message":"litellm.APIConnectionError: Credentials were refreshed, but the '
    'refreshed credentials are still expired.","type":null,"param":null,"code":"500"}}'
)
_EXPIRED_TOKEN = (
    "litellm.APIConnectionError: BedrockException - An error occurred (ExpiredTokenException) "
    "when calling the InvokeModel operation: The security token included in the request is expired"
)
_UNRECOGNIZED_CLIENT = (
    'litellm.AuthenticationError: BedrockException - {"message":"The security token included in '
    'the request is invalid.","__type":"UnrecognizedClientException"}'
)
_TUNNEL_DOWN = (
    "litellm.APIConnectionError: BedrockException - Cannot connect to host "
    "bedrock-runtime.us-east-1.amazonaws.com:443 ssl:default [Connection refused]"
)


@pytest.mark.parametrize(
    ("status", "body", "hop"),
    [
        (500, _EXPIRED_SESSION, "credentials"),
        (500, _EXPIRED_TOKEN, "credentials"),
        (401, _UNRECOGNIZED_CLIENT, "credentials"),
        (500, _TUNNEL_DOWN, "bedrock_path"),
    ],
)
def test_a_model_path_failure_fails_at_once_naming_its_hop(status: int, body: str, hop: str):
    from services.orchestration.model_path import model_unavailable_hop

    with pytest.raises(ApplicationError) as caught:
        _raise_for_status(_response(status, body))

    assert caught.value.type == "ModelUnavailable"
    assert caught.value.non_retryable is True
    assert model_unavailable_hop(caught.value) == hop
    assert f"the {hop} hop" in str(caught.value)


def test_an_unrelated_gateway_failure_stays_retryable():
    body = 'litellm.InternalServerError: BedrockException - {"message":"Model is overloaded"}'

    with pytest.raises(ApplicationError) as caught:
        _raise_for_status(_response(503, body))

    assert caught.value.type == "LiteLLMTransient"
    assert caught.value.non_retryable is False
