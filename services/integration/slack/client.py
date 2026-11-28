"""The Slack Web API client for one tenant's app, on commercial Slack or GovSlack."""

from __future__ import annotations

import logging
from typing import Any

from slack_sdk.web import WebClient

_SLACK_SDK_LOGGER = "slack_sdk"


def quiet_slack_sdk_logging() -> None:
    """Keep slack_sdk at INFO or above whatever the platform log level is.

    At DEBUG, slack_sdk logs request bodies, which carry action tokens, and Socket Mode
    envelopes, which carry ``response_url``.
    """
    sdk = logging.getLogger(_SLACK_SDK_LOGGER)
    if sdk.getEffectiveLevel() < logging.INFO:
        sdk.setLevel(logging.INFO)


def tenant_web_client(
    token: str,
    base_url: str,
    *,
    timeout: int,
    retry_handlers: list[Any] | None = None,
) -> WebClient:
    """Build the client for the tenant's API host. ``retry_handlers=[]`` disables retries."""
    quiet_slack_sdk_logging()
    return WebClient(
        token=token,
        base_url=base_url,
        timeout=timeout,
        retry_handlers=retry_handlers,
    )
