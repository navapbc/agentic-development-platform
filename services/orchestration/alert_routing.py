"""Who an alert is for, decided by who can act on it.

An administrator issue needs platform access. The administrator issues are the model path,
Temporal, the database, a defect in ADP that parks a run, sandbox and gate infrastructure,
platform configuration, limit changes, and platform-scope validation failures. Every other issue
is the tenant's, including the restart of a run.

A platform fault reaches the tenant as its effect and the statement that work resumes on its own,
and its fix goes to the administrators. A tenant run that fails on a platform error type stays
the tenant's to restart, and its cause also goes to the administrators.
"""

from __future__ import annotations

from enum import StrEnum

from services.orchestration.model_path import MODEL_UNAVAILABLE
from services.orchestration.suspension import SuspendReason

RUN_FAILED = "run_failed"


class Audience(StrEnum):
    TENANT = "tenant"
    """The tenant acts, and the administrators receive nothing."""
    PLATFORM = "platform"
    """The administrators fix it, and the tenant receives the effect only."""
    TENANT_AND_ADMIN = "tenant_and_admin"
    """The tenant acts, and the administrators receive the cause."""


PLATFORM_FAULT_REASONS = frozenset(
    {SuspendReason.AWAITING_FIX.value, SuspendReason.MODEL_UNAVAILABLE.value}
)

PLATFORM_ERROR_TYPES = frozenset(
    {
        MODEL_UNAVAILABLE,
        "LiteLLMUnreachable",
        "CoordinationAuth",
        "TenantMismatch",
        "RPCError",
        "WorkflowAlreadyStartedError",
        "OperationalError",
        "InterfaceError",
        "DBAPIError",
        "ConnectionDoesNotExistError",
        "CannotConnectNowError",
        "TooManyConnectionsError",
        "PostgresConnectionError",
        "ApiException",
        "SandboxUnavailable",
    }
)
"""The error types of Temporal, the database, the model path, coordination, and the sandbox."""


def error_type(detail: str) -> str:
    """Return the error type a run failure summary starts with, as ``<type>: <message>``."""
    head, separator, _ = detail.partition(":")
    return head.strip() if separator and " " not in head.strip() else ""


def alert_audience(reason: str, detail: str = "") -> Audience:
    if reason in PLATFORM_FAULT_REASONS:
        return Audience.PLATFORM
    if reason == RUN_FAILED and error_type(detail) in PLATFORM_ERROR_TYPES:
        return Audience.TENANT_AND_ADMIN
    return Audience.TENANT
