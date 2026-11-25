"""The platform administrator destination, read from platform-escalation.yaml.

A platform fault reaches the tenant as its effect, and its fix reaches the platform
administrators named here. The administrators are an admin Slack channel, an admin email list, or
both. Only the email set sends email only, only the channel set posts to Slack only, and a file
that sets neither, or no file, is refused, because a platform fault would then reach nobody who
can fix it.

The admin Slack channel is posted by the shared Slack app through the Slack posting service, so a
file that names a channel requires slack-shared-app.yaml. Its clicks reach the shared app's route
on the inbound webhook, which decides them under ``slack.allowlist``, whose kinds take the
``channel``, ``user_id``, and ``group_id`` forms of a tenant's escalation.yaml. The members of a
named group are recorded under ADMIN_GROUP_SCOPE.

A process reads the file from PLATFORM_ENV_CONFIG_DIR, or from config/local when that is unset, and
falls back to config/platform/ of the image or checkout when that directory does not hold it. The
cluster render projects the operator's copy into the config directory of every pod that reads it.
"""

from __future__ import annotations

import functools
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import EllipsisType
from typing import Any

import yaml

from services.integration.credentials import ConfigError, env_config_dir
from services.integration.slack.shared_app import SharedSlackApp, process_shared_app
from services.orchestration.escalation import (
    ANYONE_IN_CHANNEL,
    REPAIR_KIND,
    SHARED_APP_CHANNEL,
    Approvers,
    _allowlist,
)

log = logging.getLogger(__name__)

PLATFORM_ESCALATION_FILE = "platform-escalation.yaml"
PLATFORM_ESCALATION_VERSION = 1
ADMIN_GROUP_SCOPE = "_platform"
_REPO_ROOT = Path(__file__).resolve().parents[2]
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass(frozen=True)
class AdminDestination:
    slack_channel: str = ""
    emails: tuple[str, ...] = ()
    allowlist: Mapping[str, Approvers] = field(default_factory=dict)
    organization_id: str = ""

    @property
    def slack(self) -> bool:
        return bool(self.slack_channel)

    def approvers(self, kind: str) -> Approvers:
        return self.allowlist.get(kind, Approvers())


def platform_escalation_path() -> Path:
    projected = env_config_dir() / PLATFORM_ESCALATION_FILE
    if projected.is_file():
        return projected
    return _REPO_ROOT / "config" / "platform" / PLATFORM_ESCALATION_FILE


def _fault(message: str) -> ConfigError:
    return ConfigError(f"{PLATFORM_ESCALATION_FILE}: {message}")


def _emails(raw: Mapping[str, Any]) -> tuple[str, ...]:
    value = raw.get("email") or []
    if not isinstance(value, list) or not all(isinstance(entry, str) for entry in value):
        raise _fault("administrators.email must be a list of email addresses")
    emails = tuple(entry.strip() for entry in value if entry.strip())
    invalid = [entry for entry in emails if not _EMAIL.match(entry)]
    if invalid:
        raise _fault(f"administrators.email holds {', '.join(invalid)}, which are not addresses")
    return emails


def _slack(raw: Mapping[str, Any], shared_app: SharedSlackApp | None) -> tuple[str, Any, str]:
    value = raw.get("slack")
    if value is None:
        return "", {}, ""
    if not isinstance(value, Mapping):
        raise _fault("administrators.slack must be a mapping with a channel")
    channel = value.get("channel")
    if not isinstance(channel, str) or not channel.strip():
        return "", {}, ""
    channel = channel.strip()
    if not SHARED_APP_CHANNEL.match(channel):
        raise _fault(
            f"administrators.slack.channel {channel} is not a channel ID. The shared app posts "
            "only to a channel whose ID starts with C."
        )
    if shared_app is None:
        raise _fault(
            "administrators.slack.channel is set, and the platform directory holds no "
            "slack-shared-app.yaml. The shared Slack app posts the administrator alerts, so "
            "define it or remove administrators.slack."
        )
    raw_allowlist = value.get("allowlist") or {REPAIR_KIND: ANYONE_IN_CHANNEL}
    try:
        allowlist = _allowlist({"allowlist": raw_allowlist}, shared_app.organization_id)
    except ConfigError as exc:
        raise _fault(f"administrators.{exc}") from exc
    return channel, allowlist, shared_app.organization_id


def parse_platform_escalation(
    raw: Mapping[str, Any], shared_app: SharedSlackApp | None
) -> AdminDestination:
    version = raw.get("config_version")
    if version != PLATFORM_ESCALATION_VERSION:
        raise _fault(
            f"config_version is {version!r}, and this release reads version "
            f"{PLATFORM_ESCALATION_VERSION}. Compare it with {PLATFORM_ESCALATION_FILE}.example."
        )
    administrators = raw.get("administrators") or {}
    if not isinstance(administrators, Mapping):
        raise _fault("administrators must be a mapping with slack, email, or both")
    channel, allowlist, organization = _slack(administrators, shared_app)
    emails = _emails(administrators)
    if not channel and not emails:
        raise _fault(
            "administrators names neither slack.channel nor email, so a platform fault would "
            "reach no administrator. Set an admin Slack channel, an admin email list, or both."
        )
    return AdminDestination(
        slack_channel=channel, emails=emails, allowlist=allowlist, organization_id=organization
    )


def load_platform_escalation(
    *, shared_app: SharedSlackApp | None | EllipsisType = ..., path: Path | None = None
) -> AdminDestination:
    """Return the administrator destination, or raise ConfigError naming why it is refused."""
    target = path or platform_escalation_path()
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise _fault(
            f"{target} does not exist, so a platform fault would reach no administrator. Copy "
            f"config/platform/{PLATFORM_ESCALATION_FILE}.example to it and name an admin Slack "
            "channel, an admin email list, or both."
        ) from None
    except (OSError, UnicodeDecodeError) as exc:
        raise _fault(f"{target} could not be read. {exc}") from exc
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise _fault(f"{target} is not valid YAML. {exc}") from exc
    if not isinstance(raw, Mapping):
        raise _fault(f"{target} is not a mapping")
    return parse_platform_escalation(raw, process_shared_app() if shared_app is ... else shared_app)


@functools.cache
def _process_destination(path: Path) -> AdminDestination:
    return load_platform_escalation(path=path)


def process_admin_destination() -> AdminDestination | None:
    """Return the destination as this process first read it, or None when it is refused.

    The worker refuses to start on a refused file, so None reaches only a process that does not
    run that preflight, which logs the refusal and sends no administrator alert.
    """
    try:
        return _process_destination(platform_escalation_path())
    except ConfigError as exc:
        log.error("no administrator alert is sent, because %s", exc)
        return None


def platform_escalation_preflight() -> AdminDestination:
    return _process_destination(platform_escalation_path())
