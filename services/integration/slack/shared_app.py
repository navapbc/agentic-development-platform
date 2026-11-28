"""The shared Slack app's public settings, read from config/local/slack-shared-app.yaml.

A deployment defines at most one shared app. Its app ID, organization ID, and API base URL are
platform configuration, so the file is read only from the platform directory and never from a
tenant bundle. The app's bot token and signing secret are platform secrets and are not in the file.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from scripts.webhook_eks_render import SHARED_APP_FILE

from services.integration.credentials import (
    ConfigError,
    config_version,
    current_config_version,
    env_config_dir,
)

SHARED_APP_CONFIG = "slack_shared_app"
SLACK_API_HOST = "slack.com"
GOVSLACK_API_HOST = "slack-gov.com"


@dataclass(frozen=True)
class SharedSlackApp:
    app_id: str
    organization_id: str
    api_base_url: str


def shared_app_path() -> Path:
    return env_config_dir() / SHARED_APP_FILE


def _setting(raw: dict[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{SHARED_APP_FILE}: {key} is required and must be a string")
    return value.strip()


def load_shared_app() -> SharedSlackApp | None:
    """Return the platform's shared app, or None when the platform defines none."""
    return _read_shared_app(shared_app_path())


def process_shared_app() -> SharedSlackApp | None:
    """Return the platform's shared app as this process first read it.

    A change to the file rolls every pod that mounts it, so a process reads the file once.
    """
    return _process_shared_app(shared_app_path())


@functools.cache
def _process_shared_app(path: Path) -> SharedSlackApp | None:
    return _read_shared_app(path)


def _read_shared_app(path: Path) -> SharedSlackApp | None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{path} could not be read. {exc}") from exc
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML. {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"config file is not a mapping: {path}")
    version = config_version(raw)
    current = current_config_version(SHARED_APP_CONFIG)
    if version < current:
        raise ConfigError(
            f"{path} is version {version} but this release expects version {current}. "
            f"Compare it with {SHARED_APP_FILE}.example and add the new keys."
        )
    return SharedSlackApp(
        app_id=_setting(raw, "app_id"),
        organization_id=_setting(raw, "organization_id"),
        api_base_url=_api_base_url(raw),
    )


def _api_base_url(raw: dict[str, object]) -> str:
    """Return the API base URL, which receives the shared bot token, if it is a Slack API host."""
    url = _setting(raw, "api_base_url")
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname not in (SLACK_API_HOST, GOVSLACK_API_HOST)
        or parts.port not in (None, 443)
        or parts.username is not None
    ):
        raise ConfigError(
            f"{SHARED_APP_FILE}: api_base_url {url} is not an HTTPS URL on {SLACK_API_HOST} "
            f"or {GOVSLACK_API_HOST}, and the shared bot token goes only to the Slack API."
        )
    return url
