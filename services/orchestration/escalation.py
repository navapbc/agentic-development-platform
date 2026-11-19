"""Alert channels, and the escalation.yaml that selects a tenant's channels and Slack settings.

A tenant without escalation.yaml keeps every channel enabled, and each channel delivers when its
own settings are present. A file that lists its channels enables only those, and a file that
enables Slack names the Slack app, organization, and channel the tenant's alerts and decisions
use. The file declares the tenant's gates.yaml profile again as slack.profile, because the inbound
webhook reads escalation.yaml without gates.yaml, and the two must agree.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlsplit

from scripts.config import Settings, get_settings

from services.integration.credentials import (
    ConfigError,
    env_config_path,
    load_env_config,
    require_current_config,
)
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

FULL_CONTENT = "full"
MINIMAL_CONTENT = "minimal"
CONTENT_LEVELS = (FULL_CONTENT, MINIMAL_CONTENT)

GATE_KIND = "gate"
REPAIR_KIND = "repair"
DECISION_KINDS = (GATE_KIND, REPAIR_KIND)

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
    allowlist: Mapping[str, tuple[SlackUser, ...]] = field(default_factory=dict)

    @property
    def api_host(self) -> str:
        return host_of(self.api_base_url)

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


def _token_ttl_hours(raw: Mapping[str, Any]) -> int:
    value = raw.get("token_ttl_hours", DEFAULT_TOKEN_TTL_HOURS)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"slack.token_ttl_hours must be a positive integer, got {value!r}")
    return value


def _approved_hosts(raw: Mapping[str, Any]) -> tuple[str, ...]:
    value = raw.get("approved_hosts") or []
    if not isinstance(value, list) or not all(isinstance(host, str) for host in value):
        raise ConfigError("slack.approved_hosts must be a list of host names")
    return tuple(host.strip() for host in value if host.strip())


def _allowlist(raw: Mapping[str, Any], organization_id: str) -> dict[str, tuple[SlackUser, ...]]:
    value = raw.get("allowlist") or {}
    if not isinstance(value, Mapping):
        raise ConfigError("slack.allowlist must be a mapping of decision kind to a list of users")
    allowlist: dict[str, tuple[SlackUser, ...]] = {}
    for kind, entries in value.items():
        if kind not in DECISION_KINDS:
            raise ConfigError(
                f"slack.allowlist.{kind} is not a decision kind. Use {', '.join(DECISION_KINDS)}."
            )
        if not isinstance(entries, list):
            raise ConfigError(f"slack.allowlist.{kind} must be a list of users")
        users = []
        for index, entry in enumerate(entries):
            where = f"slack.allowlist.{kind}[{index}]"
            if not isinstance(entry, Mapping):
                raise ConfigError(f"{where} must be a mapping with a user_id")
            user_id = entry.get("user_id")
            if not isinstance(user_id, str) or not user_id.strip():
                raise ConfigError(f"{where}.user_id is required")
            organization = entry.get("organization_id", organization_id)
            if not isinstance(organization, str) or not organization.strip():
                raise ConfigError(f"{where}.organization_id must be a Slack organization ID")
            users.append(SlackUser(organization.strip(), user_id.strip()))
        allowlist[kind] = tuple(users)
    return allowlist


def _parse_slack(raw: Any) -> SlackSettings:
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ConfigError("slack must be a mapping")
    profile = _choice(raw, "profile", tuple(SHIPPED_BUNDLES))
    default_content = MINIMAL_CONTENT if profile == GOVERNMENT_PROFILE else FULL_CONTENT
    content = _text(raw, "content", required=False, default=default_content)
    if content not in CONTENT_LEVELS:
        raise ConfigError(f"slack.content must be one of {list(CONTENT_LEVELS)}, got {content!r}")
    organization_id = _text(raw, "organization_id")
    slack = SlackSettings(
        transport=_choice(raw, "transport", TRANSPORTS),
        channel=_text(raw, "channel"),
        organization_id=organization_id,
        app_id=_text(raw, "app_id"),
        profile=profile,
        content=content,
        api_base_url=_text(raw, "api_base_url", required=False, default=DEFAULT_API_BASE_URL),
        token_ttl_hours=_token_ttl_hours(raw),
        approved_hosts=_approved_hosts(raw),
        allowlist=_allowlist(raw, organization_id),
    )
    _require_profile_policy(slack)
    return slack


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


def parse_escalation_config(raw: Mapping[str, Any]) -> EscalationConfig:
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
    slack = _parse_slack(raw.get(SLACK)) if SLACK in channels else None
    return EscalationConfig(channels=channels, slack=slack)


def load_escalation_config(
    settings: Settings | None = None, *, environment: str | None = None
) -> EscalationConfig:
    """Load the tenant's escalation.yaml, refusing a slack.profile other than its gates.yaml's.

    A file whose Slack settings break the policy of their profile is refused too.
    """
    resolved = settings or get_settings()
    env = environment or resolved.environment
    if not env_config_path(ESCALATION_CONFIG, environment=env).is_file():
        return EscalationConfig()
    require_current_config(ESCALATION_CONFIG, environment=env)
    config = parse_escalation_config(load_env_config(ESCALATION_CONFIG, environment=env))
    if config.slack is None or not env_config_path(GATES_CONFIG, environment=env).is_file():
        return config
    gates_profile = load_env_config(GATES_CONFIG, environment=env).get("profile")
    return require_gates_profile(config, gates_profile)
