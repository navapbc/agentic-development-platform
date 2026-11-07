"""Bearer-token authentication in front of the MCP gateway's streamable HTTP transport."""

from __future__ import annotations

from collections.abc import Collection

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from services.http_front_door import bearer_token_accepted

AUTH_ENV_VAR = "MCP_GATEWAY_TOKEN"
AUTH_REMEDY = (
    f"Set {AUTH_ENV_VAR} in .env, then run inv k8s-deploy. A deployed gateway reads it from "
    "the mcp_gateway_token secret in Secrets Manager."
)
_POLICY_VIOLATION = 1008


class BearerTokenGate:
    """Refuse every request outside the open paths that does not present the gateway token.

    The token is compared in constant time. A refused request never reaches the MCP app, so it
    lists and invokes no tool.
    """

    def __init__(self, app: ASGIApp, token: str, open_paths: Collection[str]) -> None:
        if not token:
            raise ValueError(f"the MCP gateway has no {AUTH_ENV_VAR}. {AUTH_REMEDY}")
        self._app = app
        self._token = token
        self._open_paths = frozenset(open_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket") or scope["path"] in self._open_paths:
            await self._app(scope, receive, send)
            return
        if bearer_token_accepted(Headers(scope=scope), self._token):
            await self._app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": _POLICY_VIOLATION})
            return
        response = JSONResponse(
            {"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"}
        )
        await response(scope, receive, send)
