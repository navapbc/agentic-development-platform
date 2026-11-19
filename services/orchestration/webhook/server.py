"""HTTP entrypoint for the shared inbound webhook service.

/webhook/pr admits the GitHub repositories of every served tenant and routes each delivery to the
tenant that owns its repository. /webhook/jira/<tenant> admits the Jira projects of one tenant and
hands the issue key to that tenant's task queue. /webhook/jira serves the one tenant of a
single-tenant deployment. /webhook/slack/<tenant> takes the Slack clicks of a tenant whose Slack
transport is webhook and decides them through that tenant's dispatcher, and /webhook/slack serves
the one tenant of a single-tenant deployment. Each tenant's Slack settings load from the
escalation.yaml of its bundle and must pass the Slack policy of the profile it declares, so a
tenant without compliant settings has no Slack settings while every other tenant is served. A
webhook that serves no tenant refuses to start while Jira intake is in webhook or hybrid mode,
because no activity worker polls a task queue for the platform.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast

import yaml
from database.engine import shared_platform_engine
from scripts.config import Settings, configured_log_level, get_settings, to_host_hostport

from services.coordination.idempotency import PostgresSideEffectLedger, SideEffectLedger
from services.http_front_door import (
    REQUEST_TIMEOUT_SECONDS,
    RequestRejected,
    read_bounded_body,
)
from services.integration.credentials import ConfigError
from services.integration.integrations import load_integrations_config
from services.integration.slack.dispatcher import (
    DecisionRecords,
    PostgresDecisionRecords,
    SlackDispatcher,
    TemporalWorkflowDecisions,
    WorkflowDecisions,
)
from services.orchestration.escalation import (
    ESCALATION_CONFIG,
    GATES_CONFIG,
    EscalationConfig,
    SlackSettings,
    load_escalation_config,
    parse_escalation_config,
    require_gates_profile,
)
from services.orchestration.webhook.inbound import IntegrationHandler, handle_inbound
from services.orchestration.webhook.jira_events import (
    IssueIntakeStarter,
    TemporalIssueIntake,
    TenantJiraIntakeHandler,
)
from services.orchestration.webhook.pr_events import (
    PlatformRepositories,
    PostgresWorkflowResolver,
    PrClosedHandler,
    RepositoryTenants,
    TemporalSignaller,
    WorkflowResolver,
    WorkflowSignaller,
)
from services.orchestration.webhook.slack_events import (
    SlackInteractivity,
    SlackSettingsSource,
)
from services.orchestration.webhook.tenants import (
    ESCALATION_ACTION_KEY_ENV_PREFIX,
    GITHUB_SIGNING_ENV_PREFIX,
    JIRA_SIGNING_ENV_PREFIX,
    SLACK_SIGNING_ENV_PREFIX,
    RoutedRepositories,
    TenantRouting,
    secret_env_name,
    shared_slack_tenants,
    tenant_secrets,
)
from services.repository_intelligence.tenants import PLATFORM_TENANT, served_tenants

logging.basicConfig(level=configured_log_level())
log = logging.getLogger("inbound-webhook")

PORT = 8140
_SERVICE = "inbound-webhook"
JIRA_PATH = "/webhook/jira"
SLACK_PATH = "/webhook/slack"


@dataclass(frozen=True)
class Route:
    handler: IntegrationHandler
    allowed: Callable[[], frozenset[str]]


_INBOUND_SECRETS: dict[str, tuple[str, str, Callable[[Settings], str]]] = {
    "pr_closed": (
        "GITHUB_WEBHOOK_SECRET",
        GITHUB_SIGNING_ENV_PREFIX,
        lambda settings: settings.github_webhook_secret,
    ),
    "jira_intake": (
        "JIRA_WEBHOOK_SECRET",
        JIRA_SIGNING_ENV_PREFIX,
        lambda settings: settings.jira_webhook_secret,
    ),
}


def webhook_tenants(settings: Settings) -> list[str]:
    return served_tenants(settings) or [PLATFORM_TENANT]


def integration_secrets(
    settings: Settings, name: str, environ: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Return the signing secret of each tenant for the named integration."""
    _, prefix, read_secret = _INBOUND_SECRETS[name]
    return tenant_secrets(webhook_tenants(settings), prefix, read_secret(settings), environ)


def require_webhook_secrets(settings: Settings, environ: Mapping[str, str] | None = None) -> None:
    """Refuse to serve an integration whose inbound mode has no signing secret configured.

    With several tenants, a tenant without its own secret is served for none of its deliveries,
    and the webhook starts as long as one tenant has a secret.
    """
    integrations = load_integrations_config(settings)
    tenants = webhook_tenants(settings)
    missing: list[str] = []
    for name, (env_var, prefix, _) in sorted(_INBOUND_SECRETS.items()):
        if not integrations.webhook_enabled(name):
            continue
        secrets = integration_secrets(settings, name, environ)
        unsigned = [tenant for tenant in tenants if tenant not in secrets]
        if not secrets:
            names = [secret_env_name(prefix, tenant) for tenant in tenants if tenant]
            if len(tenants) == 1:
                names.append(env_var)
            missing.append(" or ".join(names))
        elif unsigned:
            log.warning(
                "%s refuses deliveries for tenants %s, which have no %s<TENANT> secret",
                name,
                ", ".join(unsigned),
                prefix,
            )
    if missing:
        raise SystemExit(f"set {' and '.join(missing)} to accept inbound deliveries")


def require_intake_tenant(settings: Settings) -> None:
    """Refuse to take Jira deliveries when no tenant's activity worker would take the intake.

    Every activity worker polls the task queue of its own tenant only, so an intake started for
    no tenant waits on a queue that no worker polls.
    """
    if not load_integrations_config(settings).webhook_enabled("jira_intake"):
        return
    if served_tenants(settings):
        return
    raise SystemExit(
        "jira_intake is in webhook or hybrid mode, and the webhook serves no tenant. Set TENANT "
        "or TENANTS to the tenants whose activity workers take the intake, or set the "
        "jira_intake mode to polling."
    )


@dataclass(frozen=True)
class _SlackSnapshot:
    settings: Mapping[str, SlackSettings | None]
    refused: frozenset[str]


class TenantSlackSettings:
    """The last Slack settings of each served tenant that passed the Slack policy of its profile.

    A tenant's escalation.yaml is read from its bundle, or from the environment when the bundle
    holds none, with its gates.yaml beside it. A file that fails to parse, breaks the policy, or
    names a profile other than gates.yaml's leaves the last compliant settings in force, and a
    tenant that never had compliant settings has none. A tenant that declares the Slack app ID, or
    holds the Slack signing secret, of another tenant has none either, because a request of one
    tenant's app would verify for the other.

    ``slack`` reads the last snapshot and does no I/O, so a click never waits on a bundle read.
    ``refresh`` reads every bundle and runs once before routes serve and then on a thread every
    TENANT_CONFIG_REFRESH_SECONDS.
    """

    def __init__(self, settings: Settings, routing: TenantRouting) -> None:
        self._settings = settings
        self._routing = routing
        self._interval = float(settings.tenant_config_refresh_seconds)
        self._snapshot = _SlackSnapshot({}, frozenset())
        self._refreshing: asyncio.Task[None] | None = None

    def slack(self, tenant: str) -> SlackSettings | None:
        snapshot = self._snapshot
        if tenant in snapshot.refused:
            return None
        return snapshot.settings.get(tenant)

    def refresh(self) -> None:
        tenants = self._routing.tenants()
        settings = {tenant: self._compliant(tenant) for tenant in tenants}
        secrets = tenant_secrets(
            tenants, SLACK_SIGNING_ENV_PREFIX, self._settings.slack_signing_secret
        )
        app_ids = {tenant: slack.app_id for tenant, slack in settings.items() if slack is not None}
        self._snapshot = _SlackSnapshot(settings, shared_slack_tenants(app_ids, secrets))

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

    def _compliant(self, tenant: str) -> SlackSettings | None:
        try:
            return self._load(tenant).slack
        except Exception as exc:  # noqa: BLE001
            log.error(
                "escalation.yaml of tenant %s is refused, keeping its last compliant Slack "
                "settings: %s",
                tenant or "platform",
                exc,
            )
            return self._snapshot.settings.get(tenant)

    def _load(self, tenant: str) -> EscalationConfig:
        if tenant == PLATFORM_TENANT:
            return load_escalation_config(self._settings)
        text = self._routing.bundle_text(tenant, ESCALATION_CONFIG)
        if text is None:
            return EscalationConfig()
        config = parse_escalation_config(_yaml_mapping(text, tenant, ESCALATION_CONFIG))
        gates = self._routing.bundle_text(tenant, GATES_CONFIG)
        if config.slack is None or gates is None:
            return config
        return require_gates_profile(
            config, _yaml_mapping(gates, tenant, GATES_CONFIG).get("profile")
        )


def _yaml_mapping(text: str, tenant: str, name: str) -> Mapping[str, Any]:
    raw = yaml.safe_load(text) or {}
    if not isinstance(raw, Mapping):
        raise ConfigError(f"{name}.yaml of tenant {tenant} is not a mapping")
    return raw


def pr_repositories(settings: Settings, routing: TenantRouting) -> RepositoryTenants:
    if routing.tenants() == [PLATFORM_TENANT]:
        return PlatformRepositories(settings.github_webhook_secret)
    return RoutedRepositories(routing, integration_secrets(settings, "pr_closed"))


def jira_routes(
    settings: Settings, routing: TenantRouting, starter: IssueIntakeStarter
) -> dict[str, Route]:
    tenants = routing.tenants()
    secrets = integration_secrets(settings, "jira_intake")
    routes: dict[str, Route] = {}
    for tenant in tenants:
        route = Route(
            handler=TenantJiraIntakeHandler(
                tenant=tenant, secret=secrets.get(tenant, ""), starter=starter
            ),
            allowed=lambda tenant=tenant: routing.jira_projects(tenant),
        )
        if tenant:
            routes[f"{JIRA_PATH}/{tenant}"] = route
        if len(tenants) == 1:
            routes[JIRA_PATH] = route
    return routes


def slack_routes(
    settings: Settings,
    routing: TenantRouting,
    *,
    slack: SlackSettingsSource,
    decisions: WorkflowDecisions,
    records: DecisionRecords,
) -> dict[str, SlackInteractivity]:
    """Return the Slack interactivity route of each tenant with a signing secret and action key.

    Each route serves its tenant only while that tenant's Slack transport is webhook.
    """
    tenants = routing.tenants()
    secrets = tenant_secrets(tenants, SLACK_SIGNING_ENV_PREFIX, settings.slack_signing_secret)
    action_keys = tenant_secrets(
        tenants, ESCALATION_ACTION_KEY_ENV_PREFIX, settings.escalation_action_key
    )
    routes: dict[str, SlackInteractivity] = {}
    for tenant in tenants:
        if tenant not in secrets or tenant not in action_keys:
            log.warning(
                "Slack clicks of tenant %s are refused, because it has no %s<TENANT> secret or "
                "no %s<TENANT> key",
                tenant or "platform",
                SLACK_SIGNING_ENV_PREFIX,
                ESCALATION_ACTION_KEY_ENV_PREFIX,
            )
            continue
        route = SlackInteractivity(
            tenant=tenant,
            secret=secrets[tenant],
            slack=slack,
            dispatcher=SlackDispatcher(
                tenant=tenant,
                action_key=action_keys[tenant].encode(),
                slack_settings=lambda tenant=tenant: slack(tenant),
                decisions=decisions,
                records=records,
            ),
        )
        if tenant:
            routes[f"{SLACK_PATH}/{tenant}"] = route
        if len(tenants) == 1:
            routes[SLACK_PATH] = route
    return routes


def build_routes_for(
    settings: Settings,
    *,
    routing: TenantRouting,
    signaller: WorkflowSignaller,
    starter: IssueIntakeStarter,
    resolver: WorkflowResolver | None = None,
) -> dict[str, Route]:
    pr_closed = PrClosedHandler(
        tenants=pr_repositories(settings, routing),
        resolver=resolver or PostgresWorkflowResolver(),
        signaller=signaller,
    )
    return {
        "/webhook/pr": Route(handler=pr_closed, allowed=routing.github_repositories),
        **jira_routes(settings, routing, starter),
    }


async def build_routes(settings: Settings) -> dict[str, Route | SlackInteractivity]:
    from services import temporal_client

    client = await temporal_client.connect(to_host_hostport(settings.temporal_host), settings)
    routing = TenantRouting(settings)
    slack = TenantSlackSettings(settings, routing)
    await asyncio.to_thread(slack.refresh)
    slack.start()
    return {
        **build_routes_for(
            settings,
            routing=routing,
            signaller=TemporalSignaller(client),
            starter=TemporalIssueIntake(client, settings),
        ),
        **slack_routes(
            settings,
            routing,
            slack=slack.slack,
            decisions=TemporalWorkflowDecisions(client),
            records=PostgresDecisionRecords(shared_platform_engine()),
        ),
    }


async def route_inbound(
    route: Route,
    body: bytes,
    headers: Mapping[str, str],
    *,
    ledger: SideEffectLedger,
    mode: str,
) -> tuple[int, dict[str, Any]]:
    return await handle_inbound(
        body, headers, handler=route.handler, allowed=route.allowed(), ledger=ledger, mode=mode
    )


def build_handler(
    routes: Mapping[str, Route | SlackInteractivity],
    ledger: SideEffectLedger,
    settings: Settings,
    loop: asyncio.AbstractEventLoop,
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        timeout = REQUEST_TIMEOUT_SECONDS

        def do_GET(self) -> None:
            if self.path in ("/health", "/healthz", "/"):
                self._respond(200, {"status": "ok", "service": _SERVICE})
            else:
                self._respond(404, {"error": "not found"})

        def do_POST(self) -> None:
            route = routes.get(self.path)
            if route is None:
                self._respond(404, {"error": "not found"})
                return
            try:
                body = read_bounded_body(self.rfile, self.headers)
            except RequestRejected as exc:
                self.close_connection = True
                self._respond(exc.status, {"error": exc.message})
                return
            if isinstance(route, SlackInteractivity):
                self._slack(route, body)
                return
            mode = load_integrations_config(settings).mode(route.handler.name)
            code, payload = asyncio.run_coroutine_threadsafe(
                route_inbound(
                    route,
                    body,
                    cast("dict[str, str]", self.headers),
                    ledger=ledger,
                    mode=mode,
                ),
                loop,
            ).result()
            self._respond(code, payload)

        def _slack(self, route: SlackInteractivity, body: bytes) -> None:
            reply = asyncio.run_coroutine_threadsafe(
                route.handle(body, cast("dict[str, str]", self.headers)), loop
            ).result()
            if reply.status != 200:
                self._respond(reply.status, {"error": reply.error})
                return
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
            if reply.follow_up is not None:
                asyncio.run_coroutine_threadsafe(route.send_follow_up(reply.follow_up), loop)

        def _respond(self, code: int, body: dict[str, Any]) -> None:
            encoded = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main() -> None:
    settings = get_settings()
    require_webhook_secrets(settings)
    require_intake_tenant(settings)
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, name="inbound-webhook-loop", daemon=True).start()
    routes = asyncio.run_coroutine_threadsafe(build_routes(settings), loop).result()
    ledger = PostgresSideEffectLedger()
    handler = build_handler(routes, ledger, settings, loop)
    log.info(
        "%s listening on %s:%d for %s",
        _SERVICE,
        settings.bind_host,
        PORT,
        ", ".join(sorted(routes)),
    )
    ThreadingHTTPServer((settings.bind_host, PORT), handler).serve_forever()


if __name__ == "__main__":
    main()
