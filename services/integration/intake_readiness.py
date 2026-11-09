"""Decide whether a ticket source can authenticate with this process's credentials.

The intake schedule fires every minute, so a source that cannot authenticate fails every pass.
The schedule installs paused with the reason as its note instead, and the next worker start
after the gap is fixed installs it running.
"""

from __future__ import annotations

from typing import Any

from services import process


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
        cloud = False
        for project in projects:
            credential = resolver.resolve(project)
            cloud = cloud or (credential is not None and credential.auth_mode == CLOUD)
    except SecretNotFoundError as exc:
        return f"the Jira token {exc.args[0]} has no value"
    except ConfigError as exc:
        return f"jira.yaml is not usable: {exc}"
    return acli_blocker(settings.jira_acli_path) if cloud else ""


def acli_blocker(acli_path: str) -> str:
    """Return why acli cannot run, or an empty string when it runs."""
    command = acli_path or "acli"
    try:
        process.run(
            [command, "--version"],
            timeout=process.SHORT_COMMAND_TIMEOUT_SECONDS,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return f"acli is not installed at {command}, which the Jira Cloud path needs"
    except OSError as exc:
        return f"acli at {command} cannot run on this platform ({exc.strerror})"
    return ""
