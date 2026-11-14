"""Decide whether a ticket source can authenticate with this process's credentials.

The intake schedule fires every minute, so a source that cannot authenticate fails every pass.
The schedule installs paused with the reason as its note instead, and the next worker start
after the gap is fixed installs it running.
"""

from __future__ import annotations

from typing import Any


def intake_blocker(source: str, settings: Any) -> str:
    """Return why the source cannot read tickets here, or an empty string when it can."""
    from services.integration.tickets import JIRA

    if source != JIRA:
        return ""
    return _jira_blocker(settings)


def _jira_blocker(settings: Any) -> str:
    from services.integration.credentials import ConfigError, SecretNotFoundError
    from services.integration.jira.credentials import CLOUD, build_jira_credential_resolver

    try:
        resolver = build_jira_credential_resolver(settings)
        projects = sorted(resolver.projects())
        if not projects:
            return "jira.yaml names no allowlisted project with a site, token, and board"
        credentials = [resolver.resolve(project) for project in projects]
    except SecretNotFoundError as exc:
        return f"the Jira token {exc.args[0]} has no value"
    except ConfigError as exc:
        return f"jira.yaml is not usable: {exc}"
    for credential in credentials:
        if credential is not None and credential.auth_mode == CLOUD:
            refused = _cloud_refusal(credential)
            if refused:
                return refused
    return ""


def _cloud_refusal(credential: Any) -> str:
    """Return why Jira Cloud refuses the credential, or an empty string when it accepts it.

    Only a refusal by the site counts. A network error or a server error is transient, and
    pausing the schedule for one would hold intake until the next worker start.
    """
    import httpx

    from services.integration.jira.cloud_client import JiraCloudClient, JiraCloudError

    client = JiraCloudClient(credential.url, credential.email, credential.token)
    try:
        client.myself()
    except JiraCloudError as exc:
        if exc.status is not None and 400 <= exc.status < 500:
            return f"Jira Cloud at {credential.url} refused the credential with {exc.status}"
    except httpx.HTTPError:
        return ""
    return ""
