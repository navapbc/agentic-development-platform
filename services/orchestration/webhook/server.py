"""HTTP entrypoint for the shared inbound webhook service."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast

from scripts.config import Settings, get_settings, to_host_hostport

from services.coordination.idempotency import PostgresSideEffectLedger, SideEffectLedger
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
from services.orchestration.webhook.ci_events import CIWebhookHandler
from services.orchestration.webhook.inbound import IntegrationHandler, handle_inbound
from services.orchestration.webhook.jira_events import JiraIntakeHandler
from services.orchestration.webhook.pr_events import (
    PostgresWorkflowResolver,
    PrClosedHandler,
    TemporalSignaller,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("inbound-webhook")

HOST = "0.0.0.0"  # noqa: S104
PORT = 8140
_SERVICE = "inbound-webhook"


@dataclass(frozen=True)
class Route:
    handler: IntegrationHandler
    allowed: frozenset[str]


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
    ci_status = CIWebhookHandler(
        github_secret=settings.github_webhook_secret,
        jenkins_secret=settings.jenkins_webhook_secret,
        resolver=PostgresWorkflowResolver(),
        signaller=TemporalSignaller(client),
    )
    return {
        "/webhook/pr": Route(handler=pr_closed, allowed=frozenset(allowed_repos(settings))),
        "/webhook/jira": Route(
            handler=jira_intake, allowed=frozenset(allowed_projects(settings))
        ),
        "/webhook/ci": Route(handler=ci_status, allowed=frozenset(allowed_repos(settings))),
    }


def build_handler(
    routes: dict[str, Route], ledger: SideEffectLedger, settings: Settings
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
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
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            mode = load_integrations_config(settings).mode(route.handler.name)
            code, payload = asyncio.run(
                handle_inbound(
                    body,
                    cast("dict[str, str]", self.headers),
                    handler=route.handler,
                    allowed=route.allowed,
                    ledger=ledger,
                    mode=mode,
                )
            )
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
    routes = asyncio.run(build_routes(settings))
    ledger = PostgresSideEffectLedger()
    handler = build_handler(routes, ledger, settings)
    log.info("%s listening on %s:%d for %s", _SERVICE, HOST, PORT, ", ".join(sorted(routes)))
    ThreadingHTTPServer((HOST, PORT), handler).serve_forever()


if __name__ == "__main__":
    main()
