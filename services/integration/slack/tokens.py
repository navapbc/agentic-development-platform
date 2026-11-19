"""Signed action tokens that bind a Slack button to one park of one tenant's run.

Each button carries a token in its ``value``. The token names the tenant, the execution, the
workflow, the park the alert announced, the decision kind, the actions the button may take, and
an expiry, and is signed with HMAC-SHA256 under the tenant's action key. Signing is deterministic
for a given park, so an alert activity that retries posts the same buttons.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

from services.orchestration.escalation import GATE_KIND, REPAIR_KIND
from services.orchestration.suspension import park_event_id

DECISION_EVENT = "HUMAN_INTERVENTION_DECISION"
KIND_ACTIONS = {
    GATE_KIND: frozenset({"approve", "reject"}),
    REPAIR_KIND: frozenset({"retry", "abandon"}),
}
MAX_VALUE_LENGTH = 2000
"""Slack truncates a button value longer than this."""

_VERSION = "v1"
_SIGNING_LABEL = b"adp-slack-action:"
_NONCE_LABEL = b"adp-slack-park:"


class TokenRefused(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


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

    @property
    def stage(self) -> str:
        return self.park_key.split(":", 2)[1]

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


def sign_action_token(token: ActionToken, key: bytes) -> str:
    nonce = hmac.new(key, _NONCE_LABEL + token.park_key.encode(), hashlib.sha256).hexdigest()
    body = {
        "t": token.tenant,
        "e": token.execution_id,
        "w": token.workflow_id,
        "k": token.park_key,
        "d": token.kind,
        "a": list(token.actions),
        "x": token.expires_at,
        "n": nonce[:16],
    }
    payload = _b64(json.dumps(body, separators=(",", ":"), sort_keys=True).encode())
    value = f"{_VERSION}.{payload}.{_signature(payload, key)}"
    if len(value) > MAX_VALUE_LENGTH:
        raise ValueError(f"an action token of {len(value)} characters does not fit a button")
    return value


def verify_action_token(value: str, key: bytes, *, now: float | None = None) -> ActionToken:
    """Return the token's bindings, or raise ``TokenRefused`` naming why it is refused."""
    parts = value.split(".")
    if len(parts) != 3 or parts[0] != _VERSION:
        raise TokenRefused("invalid")
    _, payload, signature = parts
    if not hmac.compare_digest(_signature(payload, key), signature):
        raise TokenRefused("invalid")
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
        )
    except (binascii.Error, ValueError, KeyError, TypeError) as exc:
        raise TokenRefused("invalid") from exc
    if token.expires_at <= (time.time() if now is None else now):
        raise TokenRefused("expired")
    if not token.park_key or token.park_key.count(":") < 3:
        raise TokenRefused("keyless")
    permitted = KIND_ACTIONS.get(token.kind)
    if permitted is None or not token.actions or not set(token.actions) <= permitted:
        raise TokenRefused("unknown_kind")
    return token
