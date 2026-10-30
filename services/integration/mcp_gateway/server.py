"""Read-only MCP Gateway serving verified read tools over streamable HTTP."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from scripts.config import get_settings

from services.integration.mcp_gateway.tools import (
    READ_TOOL_DESCRIPTIONS,
    READ_TOOL_NAMES,
    ReadOnlyToolset,
)

PORT = 8150


def build_server(
    toolset: ReadOnlyToolset | None = None, *, host: str | None = None, port: int = PORT
) -> FastMCP:
    catalog = toolset or ReadOnlyToolset.from_settings()
    server = FastMCP(name="agentic-mcp-gateway", host=host or get_settings().bind_host, port=port)
    for name in READ_TOOL_NAMES:
        server.add_tool(getattr(catalog, name), name=name, description=READ_TOOL_DESCRIPTIONS[name])
    return server


def main() -> None:
    build_server().run(transport="streamable-http")


if __name__ == "__main__":
    main()
