"""Non-interactive acli authentication bootstrap for the Cloud Jira path."""

from __future__ import annotations

import time
from urllib.parse import urlsplit

from services import process
from services.integration.jira.credentials import JiraProjectCredential

_AUTH_TTL_SECONDS = 60.0
_last_ensured: dict[str, float] = {}


class AcliNotInstalledError(RuntimeError):
    """The Jira Cloud path needs acli and the configured binary is absent."""


def _acli_command(acli_path: str) -> list[str]:
    return [acli_path] if acli_path else ["acli"]


def _site_host(url: str) -> str:
    split = urlsplit(url if "://" in url else f"//{url}")
    return split.netloc or split.path


def acli_is_authenticated(*, acli_path: str = "") -> bool:
    try:
        result = process.run(
            [*_acli_command(acli_path), "jira", "auth", "status"],
            timeout=process.SHORT_COMMAND_TIMEOUT_SECONDS,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise AcliNotInstalledError(
            f"acli is not installed at {acli_path or 'acli on PATH'}. The Jira Cloud path needs "
            "it. Rebuild the worker images with INSTALL_ACLI=true in .env, or run inv fetch-acli "
            "for a host run."
        ) from exc
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
    result = process.run(
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
        timeout=process.SHORT_COMMAND_TIMEOUT_SECONDS,
        input=cred.token,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"acli login failed: {result.stderr.strip() or result.stdout.strip()}")
    return True


def ensure_acli_authenticated_cached(
    cred: JiraProjectCredential,
    *,
    acli_path: str = "",
    ttl_seconds: float = _AUTH_TTL_SECONDS,
) -> bool:
    key = f"{_site_host(cred.url)}|{acli_path}"
    now = time.monotonic()
    last = _last_ensured.get(key)
    if last is not None and now - last < ttl_seconds:
        return False
    ensure_acli_authenticated(cred, acli_path=acli_path)
    _last_ensured[key] = now
    return True
