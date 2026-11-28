"""The response_url origin a tenant's Slack clicks may be answered through.

Every tenant calls a response_url only over HTTPS under /actions/ on the hooks host of its Slack
API, hooks.slack-gov.com for GovSlack and hooks.slack.com otherwise, and a government tenant only
when that hooks host is approved too. The hosts and content a profile permits are enforced when
escalation.yaml is parsed.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from services.integration.slack.shared_app import GOVSLACK_API_HOST
from services.orchestration.escalation import GOVERNMENT_PROFILE, SlackSettings, host_of

GOVSLACK_HOOKS_HOST = "hooks.slack-gov.com"
SLACK_HOOKS_HOST = "hooks.slack.com"
ACTIONS_PATH = "/actions/"


def _hooks_host(api_host: str) -> str:
    return GOVSLACK_HOOKS_HOST if api_host == GOVSLACK_API_HOST else SLACK_HOOKS_HOST


def hooks_url_allowed(url: str, api_host: str) -> bool:
    """Return whether the URL is an HTTPS response_url under /actions/ on the API's hooks host."""
    parts = urlsplit(url)
    return (
        parts.scheme == "https"
        and host_of(url) == _hooks_host(api_host)
        and parts.port in (None, 443)
        and parts.path.startswith(ACTIONS_PATH)
    )


def response_url_allowed(url: str, slack: SlackSettings) -> bool:
    """Return whether the tenant may call this response_url."""
    if not hooks_url_allowed(url, slack.api_host):
        return False
    return slack.profile != GOVERNMENT_PROFILE or slack.approves(_hooks_host(slack.api_host))
