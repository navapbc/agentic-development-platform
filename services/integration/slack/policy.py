"""The response_url origin a tenant's Slack clicks may be answered through.

Every tenant calls a response_url only over HTTPS under /actions/ on the hooks host of its Slack
API, hooks.slack-gov.com for GovSlack and hooks.slack.com otherwise, and a government tenant only
when that hooks host is approved too. The hosts and content a profile permits are enforced when
escalation.yaml is parsed.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from services.orchestration.escalation import GOVERNMENT_PROFILE, SlackSettings, host_of

GOVSLACK_API_HOST = "slack-gov.com"
GOVSLACK_HOOKS_HOST = "hooks.slack-gov.com"
SLACK_HOOKS_HOST = "hooks.slack.com"
ACTIONS_PATH = "/actions/"


def response_url_allowed(url: str, slack: SlackSettings) -> bool:
    """Return whether the tenant may call this response_url."""
    parts = urlsplit(url)
    hooks_host = GOVSLACK_HOOKS_HOST if slack.api_host == GOVSLACK_API_HOST else SLACK_HOOKS_HOST
    if parts.scheme != "https" or host_of(url) != hooks_host:
        return False
    if parts.port not in (None, 443) or not parts.path.startswith(ACTIONS_PATH):
        return False
    return slack.profile != GOVERNMENT_PROFILE or slack.approves(hooks_host)
