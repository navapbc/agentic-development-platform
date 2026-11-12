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
