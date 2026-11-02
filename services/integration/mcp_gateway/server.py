"""Read-only MCP Gateway serving verified read tools over streamable HTTP."""

from __future__ import annotations

from collections.abc import Sequence

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from scripts.config import get_settings
from starlette.requests import Request
from starlette.responses import JSONResponse

from services.integration.mcp_gateway.tools import (
    READ_TOOL_DESCRIPTIONS,
    READ_TOOL_NAMES,
    ReadOnlyToolset,
)

PORT = 8150
HEALTH_PATHS = ("/health", "/healthz", "/")


def build_server(
    toolset: ReadOnlyToolset | None = None,
    *,
    host: str | None = None,
    port: int = PORT,
    allowed_hosts: Sequence[str] | None = None,
) -> FastMCP:
    settings = get_settings()
    catalog = toolset or ReadOnlyToolset.from_settings()
    hosts = settings.mcp_allowed_host_values() if allowed_hosts is None else tuple(allowed_hosts)
    server = FastMCP(
        name="agentic-mcp-gateway",
        host=host or settings.bind_host,
        port=port,
        transport_security=_transport_security(hosts),
    )
    for name in READ_TOOL_NAMES:
        server.add_tool(getattr(catalog, name), name=name, description=READ_TOOL_DESCRIPTIONS[name])
    _register_health_routes(server)
    return server


def _transport_security(allowed_hosts: tuple[str, ...]) -> TransportSecuritySettings | None:
    """Build DNS rebinding protection for the configured hosts, or None for the FastMCP default.

    FastMCP turns the protection on by itself only when the server binds a loopback address.
    The platform image binds 0.0.0.0, so a deployed gateway is protected only through this
    setting. The derived origins use https because both Ingress resources are HTTPS-only.
    """
    if not allowed_hosts:
        return None
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(allowed_hosts),
        allowed_origins=[f"https://{host}" for host in allowed_hosts],
    )


def _register_health_routes(server: FastMCP) -> None:
    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "service": "mcp-gateway"})

    for path in HEALTH_PATHS:
        server.custom_route(path, methods=["GET"])(health)


def main() -> None:
    build_server().run(transport="streamable-http")


if __name__ == "__main__":
    main()
