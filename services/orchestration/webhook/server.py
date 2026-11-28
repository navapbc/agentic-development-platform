"""HTTP entrypoint for the shared inbound webhook service.

/webhook/pr admits the GitHub repositories of every served tenant and routes each delivery to the
tenant that owns its repository. /webhook/jira/<tenant> admits the Jira projects of one tenant and
hands the issue key to that tenant's task queue. /webhook/jira serves the one tenant of a
single-tenant deployment. /webhook/slack/<tenant> takes the Slack clicks of a tenant whose Slack
transport is webhook and decides them through that tenant's dispatcher, and /webhook/slack serves
the one tenant of a single-tenant deployment. /webhook/slack/_shared takes the clicks of the shared
Slack app, served when the environment defines the shared app and SLACK_SHARED_SIGNING_SECRET is
set, and decides each through the dispatcher of the tenant on the shared app whose escalation.yaml
names the click's channel, or under the administrator allowlist of platform-escalation.yaml when
the click comes from its administrator channel. An admitted alert click is taken on the tenant's
worker. With SLACK_SHARED_APP_TOKEN set, the shared app's clicks arrive over a
Socket Mode connection the webhook opens to Slack instead, and are decided the same way. Each
tenant's Slack settings load from the escalation.yaml of its bundle and must pass the Slack policy
of the profile it declares, so a tenant without compliant settings has no Slack settings while
every other tenant is served. A webhook that serves no tenant refuses to start while Jira intake is
in webhook or hybrid mode, because no activity worker polls a task queue for the platform.
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

from database.engine import shared_platform_engine
from scripts.config import Settings, configured_log_level, get_settings, to_host_hostport

from services.coordination.idempotency import PostgresSideEffectLedger, SideEffectLedger
from services.http_front_door import (
    REQUEST_TIMEOUT_SECONDS,
    RequestRejected,
    read_bounded_body,
)
from services.integration.integrations import IntegrationsConfig
from services.integration.slack.approver_groups import admin_group_refresh, keep_approver_groups
from services.integration.slack.dispatcher import (
    AlertActions,
    DecisionRecords,
    PostgresDecisionRecords,
    SlackDispatcher,
    TemporalWorkflowDecisions,
    WorkflowDecisions,
)
from services.integration.slack.tenant_settings import TenantSlackSettings
from services.integration.tenant_secrets import (
    ESCALATION_ACTION_KEY_ENV_PREFIX,
    SLACK_SIGNING_ENV_PREFIX,
    secret_env_name,
    tenant_secrets,
)
from services.orchestration.alert_actions import ForwardedAlertActions
from services.orchestration.platform_escalation import process_admin_destination
from services.orchestration.webhook.inbound import IntegrationHandler, handle_inbound
from services.orchestration.webhook.jira_events import (
    IssueIntakeStarter,
    TemporalIssueIntake,
    TenantJiraIntakeHandler,
)
from services.orchestration.webhook.pr_events import (
    PostgresWorkflowResolver,
    PrClosedHandler,
    RepositoryTenants,
    TemporalSignaller,
    WorkflowResolver,
    WorkflowSignaller,
)
from services.orchestration.webhook.slack_events import (
    AdminDestinationSource,
    ChannelTenantSource,
    RetainedHolderSource,
    SharedAppSource,
    SharedSlackInteractivity,
    SlackInteractivity,
    SlackRoute,
    SlackSettingsSource,
    shared_app_settings,
)
from services.orchestration.webhook.slack_socket import SharedSlackSocket
from services.orchestration.webhook.tenants import (
    GITHUB_SIGNING_ENV_PREFIX,
    JIRA_SIGNING_ENV_PREFIX,
    RoutedRepositories,
    TenantRouting,
)
from services.repository_intelligence.tenants import served_tenants

logging.basicConfig(level=configured_log_level())
log = logging.getLogger("inbound-webhook")

PORT = 8140
_SERVICE = "inbound-webhook"
JIRA_PATH = "/webhook/jira"
SLACK_PATH = "/webhook/slack"
SHARED_SLACK_PATH = f"{SLACK_PATH}/_shared"


@dataclass(frozen=True)
class Route:
    handler: IntegrationHandler
    allowed: Callable[[], frozenset[str]]
    mode: Callable[[str], str | None]


_INBOUND_SECRETS = {"pr_closed": GITHUB_SIGNING_ENV_PREFIX, "jira_intake": JIRA_SIGNING_ENV_PREFIX}


def webhook_tenants(settings: Settings) -> list[str]:
    tenants = served_tenants(settings)
    if not tenants:
        raise SystemExit("the webhook serves no tenant. Set TENANTS to the deployment's tenants.")
    return tenants


def integration_secrets(
    settings: Settings, name: str, environ: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Return the signing secret of each tenant for the named integration."""
    return tenant_secrets(webhook_tenants(settings), _INBOUND_SECRETS[name], environ)


def require_webhook_secrets(
    settings: Settings,
    environ: Mapping[str, str] | None = None,
    *,
    modes: Callable[[str], IntegrationsConfig | None],
) -> None:
    """Refuse to serve an integration that a tenant delivers by webhook and no tenant can sign.

    A tenant without its own secret is served for none of its deliveries, and the webhook starts as
    long as one tenant whose mode takes the integration's deliveries has a secret. A tenant whose
    modes are not yet loaded is left out, and its deliveries are answered 503 until they load.
    """
    loaded = {tenant: modes(tenant) for tenant in webhook_tenants(settings)}
    missing: list[str] = []
    for name, prefix in sorted(_INBOUND_SECRETS.items()):
        tenants = [
            tenant
            for tenant, config in loaded.items()
            if config is not None and config.webhook_enabled(name)
        ]
        if not tenants:
            continue
        secrets = integration_secrets(settings, name, environ)
        unsigned = [tenant for tenant in tenants if tenant not in secrets]
        if not secrets:
            missing.append(" or ".join(secret_env_name(prefix, tenant) for tenant in tenants))
        elif unsigned:
            log.warning(
                "%s refuses deliveries for tenants %s, which have no %s<TENANT> secret",
                name,
                ", ".join(unsigned),
                prefix,
            )
    if missing:
        raise SystemExit(f"set {' and '.join(missing)} to accept inbound deliveries")


def pr_repositories(settings: Settings, routing: TenantRouting) -> RepositoryTenants:
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
            mode=lambda tenant: routing.mode(tenant, "jira_intake"),
        )
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
    alert_actions: AlertActions | None = None,
) -> dict[str, SlackInteractivity]:
    """Return the Slack interactivity route of each tenant with a signing secret and action key.

    Each route serves its tenant only while that tenant's Slack transport is webhook.
    """
    tenants = routing.tenants()
    secrets = tenant_secrets(tenants, SLACK_SIGNING_ENV_PREFIX)
    action_keys = tenant_secrets(tenants, ESCALATION_ACTION_KEY_ENV_PREFIX)
    routes: dict[str, SlackInteractivity] = {}
    for tenant in tenants:
        if tenant not in secrets or tenant not in action_keys:
            log.warning(
                "tenant %s has no Slack interactivity route of its own, because it has no "
                "%s<TENANT> secret or no %s<TENANT> key. A tenant on the shared Slack app "
                "receives its clicks through the shared app's route",
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
                alert_actions=alert_actions,
            ),
        )
        if tenant:
            routes[f"{SLACK_PATH}/{tenant}"] = route
        if len(tenants) == 1:
            routes[SLACK_PATH] = route
    return routes


def shared_slack_routes(
    settings: Settings,
    routing: TenantRouting,
    *,
    slack: SlackSettingsSource,
    channel_tenant: ChannelTenantSource,
    shared_app: SharedAppSource,
    decisions: WorkflowDecisions,
    records: DecisionRecords,
    retained_holder: RetainedHolderSource,
) -> dict[str, SharedSlackInteractivity]:
    """Return the shared Slack app's route when the shared app's signing secret is set."""
    if not settings.slack_shared_signing_secret:
        return {}
    route = shared_slack_interactivity(
        settings,
        routing,
        slack=slack,
        channel_tenant=channel_tenant,
        shared_app=shared_app,
        decisions=decisions,
        records=records,
        retained_holder=retained_holder,
    )
    return {SHARED_SLACK_PATH: route}


def shared_slack_interactivity(
    settings: Settings,
    routing: TenantRouting,
    *,
    slack: SlackSettingsSource,
    channel_tenant: ChannelTenantSource,
    shared_app: SharedAppSource,
    decisions: WorkflowDecisions,
    records: DecisionRecords,
    retained_holder: RetainedHolderSource,
    alert_actions: AlertActions | None = None,
    admin: AdminDestinationSource = lambda: None,
) -> SharedSlackInteractivity:
    """Return the shared Slack app's click handling, with a dispatcher for each served tenant."""
    action_keys = tenant_secrets(routing.tenants(), ESCALATION_ACTION_KEY_ENV_PREFIX)
    dispatchers = {
        tenant: SlackDispatcher(
            tenant=tenant,
            action_key=key.encode(),
            slack_settings=lambda tenant=tenant: shared_app_settings(slack, tenant),
            decisions=decisions,
            records=records,
            alert_actions=alert_actions,
        )
        for tenant, key in action_keys.items()
    }
    return SharedSlackInteractivity(
        route=SHARED_SLACK_PATH,
        secret=settings.slack_shared_signing_secret,
        shared_app=shared_app,
        slack=slack,
        channel_tenant=channel_tenant,
        dispatchers=dispatchers,
        records=records,
        retained_holder=retained_holder,
        admin=admin,
    )


def shared_slack_delivery(
    settings: Settings,
    route: SharedSlackInteractivity,
    shared_app: SharedAppSource,
    loop: asyncio.AbstractEventLoop,
) -> dict[str, SharedSlackInteractivity]:
    """Start the Socket Mode consumer with SLACK_SHARED_APP_TOKEN, and return the request URL
    route to serve with SLACK_SHARED_SIGNING_SECRET."""
    if settings.slack_shared_app_token:
        SharedSlackSocket(route, settings.slack_shared_app_token, shared_app, loop).start()
    return {SHARED_SLACK_PATH: route} if settings.slack_shared_signing_secret else {}


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
        "/webhook/pr": Route(
            handler=pr_closed,
            allowed=routing.github_repositories,
            mode=lambda tenant: routing.mode(tenant, "pr_closed"),
        ),
        **jira_routes(settings, routing, starter),
    }


async def build_routes(settings: Settings, routing: TenantRouting) -> dict[str, Route | SlackRoute]:
    from services import temporal_client

    client = await temporal_client.connect(to_host_hostport(settings.temporal_host), settings)
    slack = TenantSlackSettings(settings, routing)
    await slack.refresh_then_start()
    decisions = TemporalWorkflowDecisions(client)
    records = PostgresDecisionRecords(shared_platform_engine())
    alert_actions = ForwardedAlertActions(client, settings)
    shared: dict[str, SharedSlackInteractivity] = {}
    if settings.slack_shared_signing_secret or settings.slack_shared_app_token:
        route = shared_slack_interactivity(
            settings,
            routing,
            slack=slack.slack,
            channel_tenant=slack.channel_tenant,
            shared_app=slack.shared_app,
            decisions=decisions,
            records=records,
            retained_holder=slack.retained_holder,
            alert_actions=alert_actions,
            admin=process_admin_destination,
        )
        shared = shared_slack_delivery(
            settings, route, slack.shared_app, asyncio.get_running_loop()
        )
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
            decisions=decisions,
            records=records,
            alert_actions=alert_actions,
        ),
        **shared,
    }


async def route_inbound(
    route: Route,
    body: bytes,
    headers: Mapping[str, str],
    *,
    ledger: SideEffectLedger,
) -> tuple[int, dict[str, Any]]:
    return await handle_inbound(
        body,
        headers,
        handler=route.handler,
        allowed=await asyncio.to_thread(route.allowed),
        ledger=ledger,
        mode=route.mode,
    )


def build_handler(
    routes: Mapping[str, Route | SlackRoute],
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
            if isinstance(route, SlackRoute):
                self._slack(route, body)
                return
            code, payload = asyncio.run_coroutine_threadsafe(
                route_inbound(
                    route,
                    body,
                    cast("dict[str, str]", self.headers),
                    ledger=ledger,
                ),
                loop,
            ).result()
            self._respond(code, payload)

        def _slack(self, route: SlackRoute, body: bytes) -> None:
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


async def keep_admin_groups(settings: Settings) -> None:
    """Refresh the administrator groups on the webhook's event loop.

    The refresh is built here rather than on the main thread, because its database engine is
    bound to the loop that is running when it is created.
    """
    refresh = admin_group_refresh(settings)
    if refresh is not None:
        await keep_approver_groups(refresh)


def _log_admin_group_failure(future: Any) -> None:
    if not future.cancelled() and future.exception() is not None:
        log.error("the administrator group refresh stopped", exc_info=future.exception())


def main() -> None:
    settings = get_settings()
    routing = TenantRouting(settings)
    require_webhook_secrets(settings, modes=routing.integrations)
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, name="inbound-webhook-loop", daemon=True).start()
    routes = asyncio.run_coroutine_threadsafe(build_routes(settings, routing), loop).result()
    asyncio.run_coroutine_threadsafe(keep_admin_groups(settings), loop).add_done_callback(
        _log_admin_group_failure
    )
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
