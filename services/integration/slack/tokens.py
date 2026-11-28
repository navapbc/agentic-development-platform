"""Signed action tokens that bind a Slack button to one park of one tenant's run.

Each button carries a token in its ``value``. The token names the tenant, the execution, the
workflow, the park the alert announced, the decision kind, the actions the button may take, and
an expiry, and is signed with HMAC-SHA256 under the tenant's action key. Signing is deterministic
for a given park, so an alert activity that retries posts the same buttons.

A token of an alert action names the alert in place of a park, as ``<subject>:<id>``. The subject
is ``model_path`` with the outage episode, ``run`` with the execution, ``ticket`` with the issue
key and the alert's issue time, or ``limit`` with the limit increase request. Every alert action
is of the ``repair`` kind, which admits every alert action other than a gate decision.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from enum import StrEnum

from services.orchestration.escalation import GATE_KIND, REPAIR_KIND
from services.orchestration.suspension import park_event_id

DECISION_EVENT = "HUMAN_INTERVENTION_DECISION"
KIND_ACTIONS = {
    GATE_KIND: frozenset({"approve", "reject"}),
    REPAIR_KIND: frozenset({"retry", "abandon"}),
}
ACKNOWLEDGE = "acknowledge"
VALIDATE = "validate"
FIX_DONE = "fix_done"
RESTART = "restart"
GRANT_LIMIT = "grant_limit"
DENY_LIMIT = "deny_limit"
REQUEST_LIMIT = "request_limit"
PAUSE_JOBS = "pause_jobs"
RESUME_JOBS = "resume_jobs"
ALERT_KIND_ACTIONS = {
    REPAIR_KIND: frozenset(
        {
            ACKNOWLEDGE,
            VALIDATE,
            FIX_DONE,
            RESTART,
            GRANT_LIMIT,
            DENY_LIMIT,
            REQUEST_LIMIT,
            PAUSE_JOBS,
            RESUME_JOBS,
        }
    )
}
MODEL_PATH_ALERT = "model_path"
RUN_ALERT = "run"
TICKET_ALERT = "ticket"
LIMIT_ALERT = "limit"
LIMIT_REQUEST_ALERT = "limit_request"
TENANT_JOBS_ALERT = "tenant_jobs"
ALERT_SUBJECTS = frozenset(
    {MODEL_PATH_ALERT, RUN_ALERT, TICKET_ALERT, LIMIT_ALERT, LIMIT_REQUEST_ALERT, TENANT_JOBS_ALERT}
)
MAX_VALUE_LENGTH = 2000
"""Slack truncates a button value longer than this."""

_VERSION = "v1"
_SIGNING_LABEL = b"adp-slack-action:"
_NONCE_LABEL = b"adp-slack-park:"


class TokenRefusal(StrEnum):
    INVALID = "invalid"
    EXPIRED = "expired"
    KEYLESS = "keyless"
    UNKNOWN_KIND = "unknown_kind"


class TokenRefused(Exception):
    def __init__(self, reason: TokenRefusal, token: ActionToken | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.token = token
        """The verified bindings of an expired token, which a refresh of its park reads."""


@dataclass(frozen=True)
class ActionToken:
    tenant: str
    execution_id: str
    workflow_id: str
    park_key: str
    """``<execution>:<stage>:<gate list or repair attempt>:<park ordinal>``, as the workflow
    builds it."""
    kind: str
    actions: tuple[str, ...]
    expires_at: int
    alert: str = ""
    """The alert an alert action acts on, as ``<subject>:<id>``, and empty for a park decision."""

    @property
    def stage(self) -> str:
        parts = self.park_key.split(":", 2)
        return parts[1] if len(parts) > 1 else ""

    @property
    def alert_subject(self) -> str:
        return self.alert.partition(":")[0]

    @property
    def alert_id(self) -> str:
        return self.alert.partition(":")[2]

    @property
    def decision_event_id(self) -> str:
        """The audit event the workflow writes when this park is decided."""
        return park_event_id(self.park_key, DECISION_EVENT)


def park_digest(park_key: str) -> str:
    return hashlib.sha256(park_key.encode()).hexdigest()[:32]


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _signature(payload: str, key: bytes) -> str:
    return _b64(hmac.new(key, _SIGNING_LABEL + payload.encode(), hashlib.sha256).digest())


def alert_ref(subject: str, identifier: str) -> str:
    return f"{subject}:{identifier}"


@dataclass(frozen=True)
class LimitRequestRef:
    usage: float
    park_key: str

    @property
    def limit_name(self) -> str:
        return self.park_key.rsplit(":", 2)[-2] if self.park_key.count(":") >= 3 else ""

    @property
    def alert_id(self) -> str:
        return f"{self.usage:g}:{self.park_key}"

    @classmethod
    def parse(cls, alert_id: str) -> LimitRequestRef | None:
        usage, _, park_key = alert_id.partition(":")
        try:
            ref = cls(usage=float(usage), park_key=park_key)
        except ValueError:
            return None
        return ref if ref.limit_name else None


def sign_action_token(token: ActionToken, key: bytes) -> str:
    subject = token.park_key or token.alert
    nonce = hmac.new(key, _NONCE_LABEL + subject.encode(), hashlib.sha256).hexdigest()
    body: dict[str, object] = {
        "t": token.tenant,
        "e": token.execution_id,
        "w": token.workflow_id,
        "k": token.park_key,
        "d": token.kind,
        "a": list(token.actions),
        "x": token.expires_at,
        "n": nonce[:16],
    }
    if token.alert:
        body["l"] = token.alert
    payload = _b64(json.dumps(body, separators=(",", ":"), sort_keys=True).encode())
    value = f"{_VERSION}.{payload}.{_signature(payload, key)}"
    if len(value) > MAX_VALUE_LENGTH:
        raise ValueError(f"an action token of {len(value)} characters does not fit a button")
    return value


def verify_action_token(value: str, key: bytes, *, now: float | None = None) -> ActionToken:
    """Return the token's bindings, or raise ``TokenRefused`` naming why it is refused."""
    parts = value.split(".")
    if len(parts) != 3 or parts[0] != _VERSION:
        raise TokenRefused(TokenRefusal.INVALID)
    _, payload, signature = parts
    if not hmac.compare_digest(_signature(payload, key), signature):
        raise TokenRefused(TokenRefusal.INVALID)
    try:
        body = json.loads(_unb64(payload))
        token = ActionToken(
            tenant=str(body["t"]),
            execution_id=str(body["e"]),
            workflow_id=str(body["w"]),
            park_key=str(body["k"]),
            kind=str(body["d"]),
            actions=tuple(str(action) for action in body["a"]),
            expires_at=int(body["x"]),
            alert=str(body.get("l") or ""),
        )
    except (binascii.Error, ValueError, KeyError, TypeError) as exc:
        raise TokenRefused(TokenRefusal.INVALID) from exc
    verified = _verified_alert(token) if token.alert else _verified_park(token)
    if token.expires_at <= (time.time() if now is None else now):
        raise TokenRefused(TokenRefusal.EXPIRED, verified)
    return verified


def _verified_park(token: ActionToken) -> ActionToken:
    if not token.park_key or token.park_key.count(":") < 3:
        raise TokenRefused(TokenRefusal.KEYLESS)
    permitted = KIND_ACTIONS.get(token.kind)
    if permitted is None or not token.actions or not set(token.actions) <= permitted:
        raise TokenRefused(TokenRefusal.UNKNOWN_KIND)
    return token


def _verified_alert(token: ActionToken) -> ActionToken:
    subject, _, identifier = token.alert.partition(":")
    if subject not in ALERT_SUBJECTS or not identifier or token.park_key:
        raise TokenRefused(TokenRefusal.INVALID)
    permitted = ALERT_KIND_ACTIONS.get(token.kind)
    if permitted is None or not token.actions or not set(token.actions) <= permitted:
        raise TokenRefused(TokenRefusal.UNKNOWN_KIND)
    return token


def peek_tenant(value: str) -> str:
    """Return the tenant a token names before its signature is verified, or an empty string.

    A route that serves the alerts of many tenants reads it only to select the key that then
    verifies the token.
    """
    parts = value.split(".")
    if len(parts) != 3:
        return ""
    try:
        body = json.loads(_unb64(parts[1]))
    except binascii.Error, ValueError, TypeError:
        return ""
    tenant = body.get("t") if isinstance(body, dict) else None
    return tenant if isinstance(tenant, str) else ""
