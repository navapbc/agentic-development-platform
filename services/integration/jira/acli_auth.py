"""Non-interactive acli authentication bootstrap for the Cloud Jira path."""

from __future__ import annotations

import subprocess
from urllib.parse import urlsplit

from scripts.config import Settings


def _acli_command(settings: Settings) -> list[str]:
    return [settings.jira_acli_path] if settings.jira_acli_path else ["acli"]


def _site_host(settings: Settings) -> str:
    raw = settings.jira_url
    split = urlsplit(raw if "://" in raw else f"//{raw}")
    return split.netloc or split.path


def acli_is_authenticated(settings: Settings) -> bool:
    result = subprocess.run(
        [*_acli_command(settings), "jira", "auth", "status"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def ensure_acli_authenticated(settings: Settings) -> bool:
    if acli_is_authenticated(settings):
        return False
    site = _site_host(settings)
    missing = [
        name
        for name, value in (
            ("jira_url", site),
            ("jira_email", settings.jira_email),
            ("jira_api_token", settings.jira_api_token),
        )
        if not value
    ]
    if missing:
        raise ValueError(f"cannot authenticate acli, missing settings: {', '.join(missing)}")
    result = subprocess.run(
        [
            *_acli_command(settings),
            "jira",
            "auth",
            "login",
            "--site",
            site,
            "--email",
            settings.jira_email,
            "--token",
        ],
        input=settings.jira_api_token,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"acli login failed: {result.stderr.strip() or result.stdout.strip()}")
    return True
