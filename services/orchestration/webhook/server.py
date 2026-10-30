"""HTTP entrypoint for the shared inbound webhook service."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Callable
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
from services.integration.github.credentials import (
    allowed_repos,
    build_github_credential_resolver,
)
from services.integration.integrations import load_integrations_config
from services.integration.jira.credentials import (
    allowed_projects,
    build_jira_credential_resolver,
)
from services.orchestration.intake_runtime import build_intake_context
from services.orchestration.webhook.inbound import IntegrationHandler, handle_inbound
from services.orchestration.webhook.jira_events import JiraIntakeHandler
from services.orchestration.webhook.pr_events import (
    PostgresWorkflowResolver,
    PrClosedHandler,
    TemporalSignaller,
)

logging.basicConfig(level=configured_log_level())
log = logging.getLogger("inbound-webhook")

PORT = 8140
_SERVICE = "inbound-webhook"


@dataclass(frozen=True)
class Route:
    handler: IntegrationHandler
    allowed: frozenset[str]


_INBOUND_SECRETS: dict[str, tuple[str, Callable[[Settings], str]]] = {
    "pr_closed": ("GITHUB_WEBHOOK_SECRET", lambda settings: settings.github_webhook_secret),
    "jira_intake": ("JIRA_WEBHOOK_SECRET", lambda settings: settings.jira_webhook_secret),
}


def require_webhook_secrets(settings: Settings) -> None:
    """Refuse to serve an integration whose inbound mode has no signing secret configured."""
    integrations = load_integrations_config(settings)
    missing = sorted(
        env_var
        for name, (env_var, read_secret) in _INBOUND_SECRETS.items()
        if integrations.webhook_enabled(name) and not read_secret(settings)
    )
    if missing:
        raise SystemExit(f"set {' and '.join(missing)} to accept inbound deliveries")


async def build_routes(settings: Settings) -> dict[str, Route]:
    from temporalio.client import Client

    client = await Client.connect(
        to_host_hostport(settings.temporal_host), namespace=settings.temporal_namespace
    )
    pr_closed = PrClosedHandler(
        secret=settings.github_webhook_secret,
        resolver=PostgresWorkflowResolver(),
        signaller=TemporalSignaller(client),
    )
    jira_resolver = build_jira_credential_resolver(settings)
    gh_resolver = build_github_credential_resolver(settings)
    jira_intake = JiraIntakeHandler(
        secret=settings.jira_webhook_secret,
        jira_resolver=jira_resolver,
        context=build_intake_context(settings, client=client, gh_resolver=gh_resolver),
    )
    return {
        "/webhook/pr": Route(handler=pr_closed, allowed=frozenset(allowed_repos(settings))),
        "/webhook/jira": Route(handler=jira_intake, allowed=frozenset(allowed_projects(settings))),
    }


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
                handle_inbound(
                    body,
                    cast("dict[str, str]", self.headers),
                    handler=route.handler,
                    allowed=route.allowed,
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
