"""Alert channels, and the escalation.yaml that selects a tenant's channels and Slack settings.

A tenant without escalation.yaml keeps every channel enabled, and each channel delivers when its
own settings are present. A file that lists its channels enables only those, and a file that
enables Slack names the Slack app, organization, and channel the tenant's alerts and decisions
use. The file declares the tenant's gates.yaml profile again as slack.profile, because the inbound
webhook reads escalation.yaml without gates.yaml, and the two must agree.

slack.app is own by default, and the tenant names its own Slack app. A tenant that sets it to
shared uses the platform's shared app, whose app, organization, and API base URL come from
slack-shared-app.yaml in the environment directory and may not be set in escalation.yaml.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import EllipsisType
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlsplit

from scripts.config import Settings, get_settings

from services.integration.credentials import (
    ConfigError,
    env_config_path,
    load_env_config,
    require_current_config,
)
from services.integration.slack.shared_app import SHARED_APP_FILE, SharedSlackApp, load_shared_app
from services.orchestration.gates.config import SHIPPED_BUNDLES

if TYPE_CHECKING:
    from services.orchestration.notifications import AlertMessage

ESCALATION_CONFIG = "escalation"
GATES_CONFIG = "gates"

JIRA = "jira"
EMAIL = "email"
SLACK = "slack"
CHANNEL_NAMES = (JIRA, EMAIL, SLACK)

WEBHOOK_TRANSPORT = "webhook"
SOCKET_TRANSPORT = "socket"
TRANSPORTS = (WEBHOOK_TRANSPORT, SOCKET_TRANSPORT)

OWN_APP = "own"
SHARED_APP = "shared"
APPS = (OWN_APP, SHARED_APP)
SHARED_APP_KEYS = ("app_id", "organization_id", "api_base_url")
SHARED_APP_CHANNEL = re.compile(r"^C[A-Z0-9]+$")

FULL_CONTENT = "full"
MINIMAL_CONTENT = "minimal"
CONTENT_LEVELS = (FULL_CONTENT, MINIMAL_CONTENT)

GATE_KIND = "gate"
REPAIR_KIND = "repair"
DECISION_KINDS = (GATE_KIND, REPAIR_KIND)

ANYONE_IN_CHANNEL = "channel"
USER_ID = "user_id"
GROUP_ID = "group_id"
GROUP_ID_PREFIX = "S"
TEAM_ID = "team_id"
TEAM_ID_PREFIX = "T"
MAX_APPROVER_GROUPS = 10

GOVERNMENT_PROFILE = "government"
DEFAULT_API_BASE_URL = "https://slack.com/api/"
DEFAULT_TOKEN_TTL_HOURS = 72


class EscalationChannel(Protocol):
    name: str

    def deliver(self, message: AlertMessage, settings: Settings, config: EscalationConfig) -> bool:
        """Deliver the message and return whether it was delivered."""
        ...


@dataclass(frozen=True)
class SlackUser:
    organization_id: str
    user_id: str


@dataclass(frozen=True)
class SlackGroup:
    """A Slack user group of approvers.

    ``team_id`` names the workspace that owns the group, which usergroups.users.list requires
    when the app's token is an org-level token, and is empty when the entry does not set it.
    """

    organization_id: str
    group_id: str
    team_id: str = ""


@dataclass(frozen=True)
class Approvers:
    """Who may decide one decision kind, anyone in the tenant's channel or the named users and
    the members of the named Slack user groups.

    A kind that slack.allowlist omits admits nobody. Every mode still requires the click to come
    from the tenant's organization and channel.
    """

    anyone_in_channel: bool = False
    users: tuple[SlackUser, ...] = ()
    groups: tuple[SlackGroup, ...] = ()

    def names_user(self, organization_id: str, user_id: str) -> bool:
        return SlackUser(organization_id, user_id) in self.users

    def group_ids(self, organization_id: str) -> tuple[str, ...]:
        return tuple(
            group.group_id for group in self.groups if group.organization_id == organization_id
        )


@dataclass(frozen=True)
class SlackSettings:
    transport: str
    channel: str
    organization_id: str
    app_id: str
    profile: str
    content: str
    api_base_url: str = DEFAULT_API_BASE_URL
    token_ttl_hours: int = DEFAULT_TOKEN_TTL_HOURS
    approved_hosts: tuple[str, ...] = ()
    allowlist: Mapping[str, Approvers] = field(default_factory=dict)
    app: str = OWN_APP

    @property
    def api_host(self) -> str:
        return host_of(self.api_base_url)

    def approvers(self, kind: str) -> Approvers:
        return self.allowlist.get(kind, Approvers())

    def approves(self, host: str) -> bool:
        return host.lower() in {approved.lower() for approved in self.approved_hosts}


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


@dataclass(frozen=True)
class EscalationConfig:
    channels: tuple[str, ...] = CHANNEL_NAMES
    slack: SlackSettings | None = None


def _text(raw: Mapping[str, Any], key: str, *, required: bool = True, default: str = "") -> str:
    value = raw.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ConfigError(f"slack.{key} is required when the slack channel is enabled")
        return default
    if not isinstance(value, str):
        raise ConfigError(f"slack.{key} must be a string")
    return value.strip()


def _choice(raw: Mapping[str, Any], key: str, choices: tuple[str, ...]) -> str:
    value = _text(raw, key)
    if value not in choices:
        raise ConfigError(f"slack.{key} must be one of {sorted(choices)}, got {value!r}")
    return value


def slack_token_ttl_hours(raw: Mapping[str, Any]) -> int:
    """Return the button lifetime of escalation.yaml's slack mapping, or refuse an invalid one."""
    value = raw.get("token_ttl_hours", DEFAULT_TOKEN_TTL_HOURS)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"slack.token_ttl_hours must be a positive integer, got {value!r}")
    return value


def _approved_hosts(raw: Mapping[str, Any]) -> tuple[str, ...]:
    value = raw.get("approved_hosts") or []
    if not isinstance(value, list) or not all(isinstance(host, str) for host in value):
        raise ConfigError("slack.approved_hosts must be a list of host names")
    return tuple(host.strip() for host in value if host.strip())


def _allowlist(raw: Mapping[str, Any], organization_id: str) -> dict[str, Approvers]:
    """Parse slack.allowlist, whose kinds each hold channel or a list of users and groups.

    Each list entry names exactly one of user_id and group_id, and optionally the
    organization_id of that user or group, which defaults to the tenant's organization. A group
    entry may set team_id, the workspace that owns the group. The kinds together may name at
    most MAX_APPROVER_GROUPS distinct groups.
    """
    value = raw.get("allowlist") or {}
    if not isinstance(value, Mapping):
        raise ConfigError(
            "slack.allowlist must be a mapping of decision kind to channel or a list of users "
            "and groups"
        )
    allowlist: dict[str, Approvers] = {}
    for kind, entries in value.items():
        if kind not in DECISION_KINDS:
            raise ConfigError(
                f"slack.allowlist.{kind} is not a decision kind. Use {', '.join(DECISION_KINDS)}."
            )
        if entries == ANYONE_IN_CHANNEL:
            allowlist[kind] = Approvers(anyone_in_channel=True)
            continue
        if not isinstance(entries, list):
            raise ConfigError(
                f"slack.allowlist.{kind} must be {ANYONE_IN_CHANNEL} or a list of users and "
                f"groups, got {entries!r}"
            )
        users: list[SlackUser] = []
        groups: list[SlackGroup] = []
        for index, entry in enumerate(entries):
            approver = _approver(entry, f"slack.allowlist.{kind}[{index}]", organization_id)
            (users if isinstance(approver, SlackUser) else groups).append(approver)
        allowlist[kind] = Approvers(users=tuple(users), groups=tuple(groups))
    group_ids = {group.group_id for approvers in allowlist.values() for group in approvers.groups}
    if len(group_ids) > MAX_APPROVER_GROUPS:
        raise ConfigError(
            f"slack.allowlist names {len(group_ids)} distinct groups, above the limit of "
            f"{MAX_APPROVER_GROUPS}"
        )
    return allowlist


def _approver(entry: Any, where: str, organization_id: str) -> SlackUser | SlackGroup:
    if not isinstance(entry, Mapping):
        raise ConfigError(f"{where} must be a mapping with a user_id or a group_id")
    named = [key for key in (USER_ID, GROUP_ID) if key in entry]
    if len(named) != 1:
        raise ConfigError(f"{where} must name exactly one of {USER_ID} and {GROUP_ID}")
    [key] = named
    identifier = entry[key]
    if not isinstance(identifier, str) or not identifier.strip():
        raise ConfigError(f"{where}.{key} is required")
    identifier = identifier.strip()
    organization = entry.get("organization_id", organization_id)
    if not isinstance(organization, str) or not organization.strip():
        raise ConfigError(f"{where}.organization_id must be a Slack organization ID")
    if key == USER_ID:
        if TEAM_ID in entry:
            raise ConfigError(f"{where}.{TEAM_ID} is allowed only on a {GROUP_ID} entry")
        return SlackUser(organization.strip(), identifier)
    if not identifier.startswith(GROUP_ID_PREFIX):
        raise ConfigError(
            f"{where}.group_id {identifier} is not a Slack user group ID, which starts with "
            f"{GROUP_ID_PREFIX}"
        )
    return SlackGroup(organization.strip(), identifier, _team_id(entry, where))


def _team_id(entry: Mapping[str, Any], where: str) -> str:
    if TEAM_ID not in entry:
        return ""
    team_id = entry[TEAM_ID]
    if not isinstance(team_id, str) or not team_id.strip().startswith(TEAM_ID_PREFIX):
        raise ConfigError(
            f"{where}.{TEAM_ID} {team_id!r} is not a Slack workspace ID, which starts with "
            f"{TEAM_ID_PREFIX}"
        )
    return team_id.strip()


def _parse_slack(raw: Any, shared_app: SharedSlackApp | None) -> SlackSettings:
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ConfigError("slack must be a mapping")
    app = _text(raw, "app", required=False, default=OWN_APP)
    if app not in APPS:
        raise ConfigError(f"slack.app must be one of {list(APPS)}, got {app!r}")
    profile = _choice(raw, "profile", tuple(SHIPPED_BUNDLES))
    default_content = MINIMAL_CONTENT if profile == GOVERNMENT_PROFILE else FULL_CONTENT
    content = _text(raw, "content", required=False, default=default_content)
    if content not in CONTENT_LEVELS:
        raise ConfigError(f"slack.content must be one of {list(CONTENT_LEVELS)}, got {content!r}")
    if app == SHARED_APP:
        transport = _choice(raw, "transport", TRANSPORTS)
        channel = _text(raw, "channel")
        shared = _require_shared_app(raw, shared_app, transport, channel)
        app_id, organization_id, api_base_url = (
            shared.app_id,
            shared.organization_id,
            shared.api_base_url,
        )
    else:
        organization_id = _text(raw, "organization_id")
        transport = _choice(raw, "transport", TRANSPORTS)
        channel = _text(raw, "channel")
        app_id = _text(raw, "app_id")
        if shared_app is not None and app_id == shared_app.app_id:
            raise ConfigError(
                f"slack.app_id {app_id} is the ID of the shared app in {SHARED_APP_FILE}. "
                "Set slack.app to shared and remove slack.app_id to use the shared app."
            )
        api_base_url = _text(raw, "api_base_url", required=False, default=DEFAULT_API_BASE_URL)
    slack = SlackSettings(
        transport=transport,
        channel=channel,
        organization_id=organization_id,
        app_id=app_id,
        profile=profile,
        content=content,
        api_base_url=api_base_url,
        token_ttl_hours=slack_token_ttl_hours(raw),
        approved_hosts=_approved_hosts(raw),
        allowlist=_allowlist(raw, organization_id),
        app=app,
    )
    _require_profile_policy(slack)
    return slack


def _require_shared_app(
    raw: Mapping[str, Any], shared_app: SharedSlackApp | None, transport: str, channel: str
) -> SharedSlackApp:
    """Return the shared app, or refuse the first way the settings break its rules."""
    for key in SHARED_APP_KEYS:
        if key in raw:
            raise ConfigError(
                f"slack.{key} is set, and a tenant on the shared app takes it from "
                f"{SHARED_APP_FILE}. Remove slack.{key}."
            )
    if transport != WEBHOOK_TRANSPORT:
        raise ConfigError(
            f"slack.transport is {transport}, and the shared app requires {WEBHOOK_TRANSPORT}."
        )
    if not SHARED_APP_CHANNEL.match(channel):
        raise ConfigError(
            f"slack.channel {channel} is not a channel ID. The shared app posts only to a "
            "channel whose ID starts with C, and refuses group, direct message, and user IDs."
        )
    if shared_app is None:
        raise ConfigError(
            f"slack.app is shared, and the environment directory holds no {SHARED_APP_FILE}. "
            f"The platform defines the shared app in {SHARED_APP_FILE}."
        )
    return shared_app


def _require_profile_policy(slack: SlackSettings) -> None:
    """Refuse Slack settings with every way they break a government profile.

    A government tenant calls only the Slack hosts it approves and sends minimal content.
    """
    if slack.profile != GOVERNMENT_PROFILE:
        return
    violations = []
    if not slack.approves(slack.api_host):
        violations.append(
            f"slack.api_base_url names host {slack.api_host or slack.api_base_url}, which "
            "slack.approved_hosts does not list. A government tenant calls only approved hosts."
        )
    if slack.content != MINIMAL_CONTENT:
        violations.append(
            f"slack.content is {slack.content}, and a government tenant requires {MINIMAL_CONTENT}."
        )
    if violations:
        raise ConfigError(" ".join(violations))


def require_gates_profile(config: EscalationConfig, gates_profile: object) -> EscalationConfig:
    """Return the config, or refuse it when slack.profile differs from the gates.yaml profile."""
    if config.slack is not None and gates_profile != config.slack.profile:
        raise ConfigError(
            f"slack.profile is {config.slack.profile} but the gates.yaml profile is "
            f"{gates_profile}. Set slack.profile to the profile gates.yaml names."
        )
    return config


def parse_escalation_config(
    raw: Mapping[str, Any], shared_app: SharedSlackApp | None = None
) -> EscalationConfig:
    channels_raw = raw.get("channels", list(CHANNEL_NAMES))
    if not isinstance(channels_raw, list):
        raise ConfigError(f"channels must be a list drawn from {list(CHANNEL_NAMES)}")
    unknown = [name for name in channels_raw if name not in CHANNEL_NAMES]
    if unknown:
        raise ConfigError(
            f"channels holds {unknown}, which are not channels. A channel is one of "
            f"{list(CHANNEL_NAMES)}."
        )
    channels = tuple(name for name in CHANNEL_NAMES if name in channels_raw)
    slack = _parse_slack(raw.get(SLACK), shared_app) if SLACK in channels else None
    return EscalationConfig(channels=channels, slack=slack)


def shared_app_tenants(settings: Settings) -> list[str]:
    """Return the served tenants, refusing none, since the shared Slack app posts only for one."""
    from services.repository_intelligence.tenants import served_tenants

    tenants = served_tenants(settings)
    if not tenants:
        raise ConfigError(
            "slack.app is shared, and neither TENANT nor TENANTS names a tenant. The shared Slack "
            "app posts only for a tenant's activity worker, so set TENANT or TENANTS, or give "
            "the platform its own Slack app"
        )
    return tenants


def load_escalation_config(
    settings: Settings | None = None,
    *,
    shared_app: SharedSlackApp | None | EllipsisType = ...,
) -> EscalationConfig:
    """Load the tenant's escalation.yaml, refusing a slack.profile other than its gates.yaml's.

    A file whose Slack settings break the policy of their profile, or the rules of the shared app,
    is refused too. The shared app is the one given, else the one the environment directory
    defines.
    """
    resolved = settings or get_settings()
    if not env_config_path(ESCALATION_CONFIG).is_file():
        return EscalationConfig()
    require_current_config(ESCALATION_CONFIG)
    config = parse_escalation_config(
        load_env_config(ESCALATION_CONFIG),
        load_shared_app() if shared_app is ... else shared_app,
    )
    if config.slack is not None and config.slack.app == SHARED_APP:
        shared_app_tenants(resolved)
    if config.slack is None or not env_config_path(GATES_CONFIG).is_file():
        return config
    gates_profile = load_env_config(GATES_CONFIG).get("profile")
    return require_gates_profile(config, gates_profile)
