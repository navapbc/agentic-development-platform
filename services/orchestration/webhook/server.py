"""HTTP entrypoint for the shared inbound webhook service.

/webhook/pr admits the GitHub repositories of every served tenant and routes each delivery to the
tenant that owns its repository. /webhook/jira/<tenant> admits the Jira projects of one tenant and
hands the issue key to that tenant's task queue. /webhook/jira serves the one tenant of a
single-tenant deployment, or the platform when no tenant is served.
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

from scripts.config import Settings, configured_log_level, get_settings, to_host_hostport

from services.coordination.idempotency import PostgresSideEffectLedger, SideEffectLedger
from services.http_front_door import (
    REQUEST_TIMEOUT_SECONDS,
    RequestRejected,
    read_bounded_body,
)
from services.integration.integrations import load_integrations_config
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
from services.orchestration.webhook.tenants import (
    GITHUB_SIGNING_ENV_PREFIX,
    JIRA_SIGNING_ENV_PREFIX,
    RoutedRepositories,
    TenantRouting,
    secret_env_name,
    tenant_secrets,
)
from services.repository_intelligence.tenants import (
    PLATFORM_TENANT,
    home_tenant,
    served_tenants,
)

logging.basicConfig(level=configured_log_level())
log = logging.getLogger("inbound-webhook")

PORT = 8140
_SERVICE = "inbound-webhook"
JIRA_PATH = "/webhook/jira"


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
        resolver=resolver or PostgresWorkflowResolver(unattributed_tenant=home_tenant(settings)),
        signaller=signaller,
    )
    return {
        "/webhook/pr": Route(handler=pr_closed, allowed=routing.github_repositories),
        **jira_routes(settings, routing, starter),
    }


async def build_routes(settings: Settings) -> dict[str, Route]:
    from services import temporal_client

    client = await temporal_client.connect(to_host_hostport(settings.temporal_host), settings)
    return build_routes_for(
        settings,
        routing=TenantRouting(settings),
        signaller=TemporalSignaller(client),
        starter=TemporalIssueIntake(client, settings),
    )


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
    routes: dict[str, Route],
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
