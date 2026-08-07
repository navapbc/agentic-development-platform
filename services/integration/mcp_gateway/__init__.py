"""Read-only MCP Gateway exposing verified read tools to agents."""

from services.integration.mcp_gateway.server import build_server
from services.integration.mcp_gateway.tools import (
    READ_TOOL_NAMES,
    ReadOnlyToolset,
)

__all__ = [
    "READ_TOOL_NAMES",
    "ReadOnlyToolset",
    "build_server",
]
