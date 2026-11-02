"""Shared-secret remedy text for the coordination RPC, used by the service and its client."""

from __future__ import annotations

SHARED_SECRET_ENV_VAR = "COORDINATION_SHARED_SECRET"

SHARED_SECRET_REMEDY = (
    f"Set {SHARED_SECRET_ENV_VAR} in .env to one value the coordination service and the "
    "workers both read, then restart the stack with inv local-up."
)


class CoordinationAuthError(RuntimeError):
    """Raised when the coordination bearer token is absent or the service refuses it."""
