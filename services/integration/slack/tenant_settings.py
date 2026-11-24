"""The Slack settings of every served tenant, and the tenant each shared app channel routes to.

The inbound webhook and the Slack posting service read one snapshot of these settings. A tenant's
escalation.yaml is read only from its bundle, and its gates.yaml from the bundle or else the
platform directory, and no other bundle file is read. The file is parsed against the shared Slack
app that slack-shared-app.yaml in the platform directory defines, read once per refresh.

Each channel that a tenant on the shared app names routes to one tenant. On the tenant config
bucket, a tenant that names such a channel is served only when its claim marker is the earliest
of every marker that contends with it, including markers of tenants that no longer name the
channel. Tenants on their own apps do not contend with each other. Without a bucket, one tenant
naming the channel is served and two or more are all refused, since the render refuses that clash.

The channel decisions use only the bundles, the platform files, the shared app, and the
markers, so the webhook and the posting service reach the same decisions. Refusals for a shared
Slack app ID or signing secret apply after them.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Protocol

import yaml
from scripts.config import Settings

from services.integration.credentials import ConfigError, env_config_dir
from services.integration.slack.shared_app import SHARED_APP_FILE, SharedSlackApp, load_shared_app
from services.integration.tenant_claims import (
    CLAIMS_PROTOCOL_ABSENT,
    CLAIMS_PROTOCOL_KEY,
    SLACK_CHANNEL,
    Claim,
    ClaimStore,
    Marker,
    claim_check,
    claims_protocol_in_force,
    slack_channel_claim,
)
from services.integration.tenant_config import (
    TENANT_REPLACEABLE_FILES,
    BundleSource,
    config_bucket_client,
)
from services.integration.tenant_secrets import (
    SLACK_SIGNING_ENV_PREFIX,
    action_key_fingerprints,
    tenant_secrets,
)
from services.orchestration.escalation import (
    ESCALATION_CONFIG,
    GATES_CONFIG,
    SHARED_APP,
    EscalationConfig,
    SlackSettings,
    parse_escalation_config,
    require_gates_profile,
)

log = logging.getLogger(__name__)

SLACK_FILES = (ESCALATION_CONFIG, GATES_CONFIG)
REFRESH_WORKERS = 8


class TenantBundles(Protocol):
    def tenants(self) -> list[str]: ...

    def source(self, tenant: str) -> BundleSource: ...


@dataclass(frozen=True)
class _Claimant:
    tenant: str
    kind: str


@dataclass(frozen=True)
class _ChannelDecision:
    holder: str | None
    served: frozenset[_Claimant]
    refused: frozenset[str]
    claimants: frozenset[_Claimant]


@dataclass
class _TenantState:
    """One tenant's last fetched Slack files and the last compliant settings parsed from them."""

    versions: dict[str, str] = field(default_factory=dict)
    texts: dict[str, str] = field(default_factory=dict)
    parsed: str = ""
    settings: SlackSettings | None = None
    error: str = ""


@dataclass(frozen=True)
class _SlackSnapshot:
    settings: Mapping[str, SlackSettings | None]
    refused: frozenset[str]
    channels: Mapping[str, str]
    shared_app: SharedSlackApp | None


class TenantSlackSettings:
    """The last Slack settings of each served tenant that passed the Slack policy of its profile.

    A file that fails to parse, breaks the policy or the shared app's rules, or names a profile
    other than gates.yaml's leaves the last compliant settings in force, and a tenant that never
    had compliant settings has none. A tenant that declares the Slack app ID of another tenant on
    its own app or of the shared app, or holds the Slack signing secret of another tenant, has
    none either, because a request of one tenant's app would verify for the other. A tenant that
    does not hold a channel it names has none.

    Each input keeps its last good result when its read fails. A shared app file that fails keeps
    the last shared app, and a claim listing that fails keeps the channel's last decision, or
    refuses the channel's tenants when it has none. A bundle file is fetched only when its version
    changed, and a tenant's files are parsed only when their content or the shared app changed.

    ``slack``, ``channel_tenant``, and ``shared_app`` read the last snapshot and do no I/O, so a
    click never waits on a bundle read. ``refresh`` reads every bundle and runs once before routes
    serve and then on a thread every TENANT_CONFIG_REFRESH_SECONDS.
    """

    def __init__(
        self, settings: Settings, bundles: TenantBundles, *, claims_client: Any = None
    ) -> None:
        self._settings = settings
        self._bundles = bundles
        self._claims_client = claims_client
        self._interval = float(settings.tenant_config_refresh_seconds)
        self._snapshot = _SlackSnapshot({}, frozenset(), {}, None)
        self._states: dict[str, _TenantState] = {}
        self._decisions: dict[str, _ChannelDecision] = {}
        self._shared_app: SharedSlackApp | None = None
        self._protocol: bool | None = None
        self._reported: dict[str, str] = {}
        self._refreshing: asyncio.Task[None] | None = None
        self._retained_holders: dict[str, str | None] = {}

    def slack(self, tenant: str) -> SlackSettings | None:
        snapshot = self._snapshot
        if tenant in snapshot.refused:
            return None
        return snapshot.settings.get(tenant)

    def channel_tenant(self, channel: str) -> str | None:
        """Return the tenant on the shared app that holds the channel, or None."""
        return self._snapshot.channels.get(channel)

    def shared_app(self) -> SharedSlackApp | None:
        return self._snapshot.shared_app

    async def retained_holder(self, channel: str) -> str | None:
        """Return the tenant on the shared app that left the channel and still holds its claim.

        The holder is the served tenant whose marker on the channel is the earliest of every
        contending marker and whose escalation.yaml names another channel. The claim listing runs
        only for a click the snapshot does not route, on a thread, and its answer serves every
        click in the channel until the next refresh. Without a bucket no tenant holds a channel it
        left.
        """
        bucket = self._settings.tenant_config_bucket
        if not bucket:
            return None
        channel = channel.strip().upper()
        holders = self._retained_holders
        if channel in holders:
            return holders[channel]
        try:
            markers = await asyncio.to_thread(self._channel_markers, bucket, channel)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "the claims of Slack channel %s could not be listed for a click (%s)",
                channel,
                type(exc).__name__,
            )
            return None
        holders[channel] = self._holder_of_left_channel(channel, markers)
        return holders[channel]

    def _holder_of_left_channel(self, channel: str, markers: dict[str, list[Marker]]) -> str | None:
        claim = Claim(SLACK_CHANNEL, channel)
        for tenant in sorted({marker.tenant for marker in markers.get(SLACK_CHANNEL, ())}):
            if not claim_check(tenant, claim, markers).held:
                continue
            slack = self.slack(tenant)
            if slack is None or slack.app != SHARED_APP or slack.channel.strip().upper() == channel:
                return None
            return tenant
        return None

    def _channel_markers(self, bucket: str, channel: str) -> dict[str, list[Marker]]:
        return ClaimStore(self._client(), bucket).contending_markers(Claim(SLACK_CHANNEL, channel))

    def refresh(self) -> None:
        shared_app = self._read_shared_app()
        fallback = self._environment_files()
        tenants = self._bundles.tenants()
        for tenant in tenants:
            self._states.setdefault(tenant, _TenantState())
        with ThreadPoolExecutor(max_workers=max(1, min(REFRESH_WORKERS, len(tenants)))) as pool:
            compliant = pool.map(
                lambda tenant: self._compliant(tenant, shared_app, fallback), tenants
            )
            settings = dict(zip(tenants, compliant, strict=True))
        channels, refused = self._channels(settings, self._claims_protocol())
        secrets = tenant_secrets(tenants, SLACK_SIGNING_ENV_PREFIX)
        fingerprints = action_key_fingerprints(tenants)
        unfingerprinted = sorted(
            tenant
            for tenant, slack in settings.items()
            if slack is not None and slack.app == SHARED_APP and tenant not in fingerprints
        )
        if len(tenants) > 1 and unfingerprinted:
            self._report_on_change(
                "action key fingerprints",
                f"tenants {', '.join(unfingerprinted)} have no escalation action key or its "
                "ESCALATION_ACTION_KEY_SHA256_<TENANT> fingerprint, so a key they share with "
                "another tenant goes undetected",
            )
        refused |= shared_slack_tenants(
            settings, secrets, shared_app.app_id if shared_app else None, fingerprints
        )
        self._snapshot = _SlackSnapshot(settings, frozenset(refused), channels, shared_app)
        self._retained_holders = {}

    async def refresh_then_start(self) -> None:
        try:
            await asyncio.to_thread(self.refresh)
        except Exception:  # noqa: BLE001
            log.exception("the first Slack settings refresh failed, so the next refresh retries it")
        self.start()

    def start(self) -> None:
        """Refresh on a thread every interval, for the life of the running event loop."""
        self._refreshing = asyncio.get_running_loop().create_task(self._refresh_every_interval())

    async def _refresh_every_interval(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                await asyncio.to_thread(self.refresh)
            except Exception:  # noqa: BLE001
                log.exception("the Slack settings refresh failed, keeping the last snapshot")

    def _report_on_change(self, subject: str, message: str) -> None:
        if self._reported.get(subject) != message:
            log.error("%s", message)
            self._reported[subject] = message

    def _read_shared_app(self) -> SharedSlackApp | None:
        try:
            self._shared_app = load_shared_app()
        except Exception as exc:  # noqa: BLE001
            self._report_on_change(
                SHARED_APP_FILE,
                f"{SHARED_APP_FILE} is refused, keeping the last shared app. {exc}",
            )
            return self._shared_app
        self._reported.pop(SHARED_APP_FILE, None)
        return self._shared_app

    def _client(self) -> Any:
        if self._claims_client is None:
            self._claims_client = config_bucket_client(self._settings)
        return self._claims_client

    def _claims_protocol(self) -> bool:
        """Return whether a tenant on the shared app may be served, as the claims-protocol object
        on the bucket says. A read that fails keeps the last answer, which is no before any read
        succeeded."""
        bucket = self._settings.tenant_config_bucket
        if not bucket:
            return True
        try:
            in_force = claims_protocol_in_force(self._client(), bucket)
        except Exception as exc:  # noqa: BLE001
            kept = "the last answer stays" if self._protocol is not None else "it is refused"
            self._report_on_change(
                CLAIMS_PROTOCOL_KEY,
                f"{CLAIMS_PROTOCOL_KEY} could not be read, so for every tenant on the shared "
                f"Slack app {kept}: {exc}",
            )
            return bool(self._protocol)
        self._reported.pop(CLAIMS_PROTOCOL_KEY, None)
        if in_force != self._protocol:
            if in_force:
                log.info(
                    "%s exists, so tenants on the shared Slack app are served", CLAIMS_PROTOCOL_KEY
                )
            else:
                log.error(
                    "%s. Every tenant on the shared Slack app is refused", CLAIMS_PROTOCOL_ABSENT
                )
        self._protocol = in_force
        return in_force

    def _environment_files(self) -> dict[str, str]:
        directory = env_config_dir()
        files = {}
        for name in SLACK_FILES:
            if name not in TENANT_REPLACEABLE_FILES:
                continue
            try:
                files[name] = (directory / f"{name}.yaml").read_text(encoding="utf-8")
            except FileNotFoundError:
                continue
        return files

    def _compliant(
        self, tenant: str, shared_app: SharedSlackApp | None, fallback: Mapping[str, str]
    ) -> SlackSettings | None:
        state = self._states[tenant]
        try:
            files = self._slack_files(tenant, state, fallback)
            parsed = _parse_key(files, shared_app)
            if parsed != state.parsed:
                state.parsed = parsed
                state.settings = _parse(tenant, files, shared_app).slack
        except Exception as exc:  # noqa: BLE001
            if str(exc) != state.error:
                log.error(
                    "escalation.yaml of tenant %s is refused, keeping its last compliant Slack "
                    "settings. %s",
                    tenant,
                    exc,
                )
                state.error = str(exc)
            return state.settings
        state.error = ""
        return state.settings

    def _slack_files(
        self, tenant: str, state: _TenantState, fallback: Mapping[str, str]
    ) -> dict[str, str]:
        """Return the tenant's escalation.yaml from the bundle and its gates.yaml from the bundle or
        else the platform directory, fetching only a file whose version changed."""
        source = self._bundles.source(tenant)
        identity = source.latest()
        versions = {name: identity.files[name] for name in SLACK_FILES if name in identity.files}
        changed = {
            name for name, version in versions.items() if state.versions.get(name) != version
        }
        fetched = source.fetch_files(identity, sorted(changed)) if changed else {}
        state.texts = {
            name: fetched[name] if name in changed else state.texts[name]
            for name in versions
            if name in fetched or name not in changed
        }
        state.versions = {name: versions[name] for name in state.texts}
        return {
            name: text
            for name in SLACK_FILES
            if (text := state.texts.get(name, fallback.get(name))) is not None
        }

    def _channels(
        self, settings: Mapping[str, SlackSettings | None], protocol: bool
    ) -> tuple[dict[str, str], set[str]]:
        """Return each channel of a tenant on the shared app mapped to the tenant that holds it,
        and every tenant that names such a channel without holding it.

        Without the claims protocol in force, every tenant on the shared app is refused.
        """
        named: dict[str, list[_Claimant]] = {}
        refused: set[str] = set()
        for tenant, slack in sorted(settings.items()):
            if slack is None:
                continue
            if slack.app == SHARED_APP and not protocol:
                refused.add(tenant)
                continue
            claim = slack_channel_claim(slack.app, slack.channel)
            named.setdefault(claim.value, []).append(_Claimant(tenant, claim.kind))
        contested = {
            channel: claimants
            for channel, claimants in sorted(named.items())
            if any(claimant.kind == SLACK_CHANNEL for claimant in claimants)
        }
        bucket = self._settings.tenant_config_bucket
        listed = self._list_markers(bucket, list(contested)) if bucket else {}
        channels: dict[str, str] = {}
        decisions: dict[str, _ChannelDecision] = {}
        for channel, claimants in contested.items():
            decision = self._decide(channel, claimants, listed.get(channel))
            if decision != self._decisions.get(channel) and decision.refused:
                _log_refusal(channel, decision)
            decisions[channel] = decision
            refused |= decision.refused
            if decision.holder is not None:
                channels[channel] = decision.holder
        self._decisions = decisions
        return channels, refused

    def _list_markers(
        self, bucket: str, channels: Sequence[str]
    ) -> dict[str, dict[str, list[Marker]] | Exception]:
        """List the contending markers of each channel on the refresh pool, keeping each
        channel's failure as its result."""

        def listing(channel: str) -> dict[str, list[Marker]] | Exception:
            try:
                return self._channel_markers(bucket, channel)
            except Exception as exc:  # noqa: BLE001
                return exc

        if not channels:
            return {}
        try:
            self._client()
        except Exception as exc:  # noqa: BLE001
            return dict.fromkeys(channels, exc)
        with ThreadPoolExecutor(max_workers=min(REFRESH_WORKERS, len(channels))) as pool:
            return dict(zip(channels, pool.map(listing, channels), strict=True))

    def _decide(
        self,
        channel: str,
        claimants: Sequence[_Claimant],
        listed: dict[str, list[Marker]] | Exception | None,
    ) -> _ChannelDecision:
        if listed is None:
            if len(claimants) == 1:
                return _decision(claimants, frozenset(claimants))
            return _decision(claimants, frozenset())
        subject = f"channel {channel}"
        if isinstance(listed, Exception):
            exc = listed
            last = self._decisions.get(channel)
            if last is not None and last.claimants != frozenset(claimants):
                last = None
            self._report_on_change(
                subject,
                f"the claims of Slack channel {channel} could not be listed, so "
                f"{'its last decision stays' if last else 'its tenants are refused'}. {exc}",
            )
            return _decision(claimants, last.served if last is not None else frozenset())
        self._reported.pop(subject, None)
        held = frozenset(
            claimant
            for claimant in claimants
            if claim_check(claimant.tenant, Claim(claimant.kind, channel), listed).held
        )
        return _decision(claimants, held)


def _decision(claimants: Sequence[_Claimant], served: frozenset[_Claimant]) -> _ChannelDecision:
    holder = next((c.tenant for c in served if c.kind == SLACK_CHANNEL), None)
    refused = frozenset(claimant.tenant for claimant in claimants if claimant not in served)
    return _ChannelDecision(holder, served, refused, frozenset(claimants))


def _log_refusal(channel: str, decision: _ChannelDecision) -> None:
    losers = ", ".join(sorted(decision.refused))
    if decision.served:
        holders = ", ".join(sorted(claimant.tenant for claimant in decision.served))
        log.error(
            "tenant %s holds the earliest claim on Slack channel %s, so the Slack settings of "
            "tenants %s, which also name it, are refused",
            holders,
            channel,
            losers,
        )
        return
    log.error(
        "tenants %s name Slack channel %s and none holds the earliest claim on it, so the Slack "
        "settings of each are refused",
        losers,
        channel,
    )


def shared_slack_tenants(
    slack: Mapping[str, SlackSettings | None],
    secrets: Mapping[str, str],
    shared_app_id: str | None,
    action_key_fingerprints: Mapping[str, str],
) -> frozenset[str]:
    """Return the tenants whose Slack requests or actions verify for another tenant.

    Tenants on the shared app verify under the shared signing secret, so their own app IDs and
    signing secrets are not compared. Any two tenants whose escalation action keys have one
    fingerprint are returned, because an action token of one would verify for the other.
    """
    on_shared_app = {
        tenant
        for tenant, settings in slack.items()
        if settings is not None and settings.app == SHARED_APP
    }
    own_app_ids = {
        tenant: settings.app_id
        for tenant, settings in slack.items()
        if settings is not None and tenant not in on_shared_app
    }
    own_app_secrets = {
        tenant: secret for tenant, secret in secrets.items() if tenant not in on_shared_app
    }
    refused: set[str] = set()
    reserved = sorted(
        tenant
        for tenant, app_id in own_app_ids.items()
        if shared_app_id and app_id == shared_app_id
    )
    if reserved:
        log.error(
            "tenants %s declare the Slack app ID of the shared app, so their Slack requests are "
            "refused",
            ", ".join(reserved),
        )
        refused.update(reserved)
    for kind, values in (
        ("Slack app ID", own_app_ids),
        ("Slack signing secret", own_app_secrets),
        ("escalation action key", action_key_fingerprints),
    ):
        holders: dict[str, list[str]] = {}
        for tenant, value in values.items():
            if value:
                holders.setdefault(value, []).append(tenant)
        for tenants in holders.values():
            if len(tenants) > 1:
                log.error(
                    "tenants %s share a %s, so their Slack requests are refused",
                    ", ".join(sorted(tenants)),
                    kind,
                )
                refused.update(tenants)
    return frozenset(refused)


def _parse_key(files: Mapping[str, str], shared_app: SharedSlackApp | None) -> str:
    digest = hashlib.sha256(repr(shared_app).encode())
    for name in SLACK_FILES:
        digest.update(f"\0{name}\0{files.get(name, '')}".encode())
    return digest.hexdigest()


def _parse(
    tenant: str, files: Mapping[str, str], shared_app: SharedSlackApp | None
) -> EscalationConfig:
    text = files.get(ESCALATION_CONFIG)
    if text is None:
        return EscalationConfig()
    config = parse_escalation_config(_yaml_mapping(text, tenant, ESCALATION_CONFIG), shared_app)
    gates = files.get(GATES_CONFIG)
    if config.slack is None or gates is None:
        return config
    return require_gates_profile(config, _yaml_mapping(gates, tenant, GATES_CONFIG).get("profile"))


def _yaml_mapping(text: str, tenant: str, name: str) -> Mapping[str, Any]:
    raw = yaml.safe_load(text) or {}
    if not isinstance(raw, Mapping):
        raise ConfigError(f"{name}.yaml of tenant {tenant} is not a mapping")
    return raw
