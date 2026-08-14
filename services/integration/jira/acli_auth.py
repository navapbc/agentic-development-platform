"""Non-interactive acli authentication bootstrap for the Cloud Jira path."""

from __future__ import annotations

import subprocess
from urllib.parse import urlsplit

from services.integration.jira.credentials import JiraProjectCredential


def _acli_command(acli_path: str) -> list[str]:
    return [acli_path] if acli_path else ["acli"]


def _site_host(url: str) -> str:
    split = urlsplit(url if "://" in url else f"//{url}")
    return split.netloc or split.path


def acli_is_authenticated(*, acli_path: str = "") -> bool:
    result = subprocess.run(
        [*_acli_command(acli_path), "jira", "auth", "status"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def ensure_acli_authenticated(cred: JiraProjectCredential, *, acli_path: str = "") -> bool:
    if acli_is_authenticated(acli_path=acli_path):
        return False
    site = _site_host(cred.url)
    missing = [
        name
        for name, value in (
            ("jira_url", site),
            ("jira_email", cred.email),
            ("jira_api_token", cred.token),
        )
        if not value
    ]
    if missing:
        raise ValueError(f"cannot authenticate acli, missing settings: {', '.join(missing)}")
    result = subprocess.run(
        [
            *_acli_command(acli_path),
            "jira",
            "auth",
            "login",
            "--site",
            site,
            "--email",
            cred.email,
            "--token",
        ],
        input=cred.token,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"acli login failed: {result.stderr.strip() or result.stdout.strip()}")
    return True
