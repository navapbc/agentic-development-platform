"""Real OPA backend: promotion decisions evaluated over the OPA REST data API.

The client posts the assembled decision input to the authored `agentic.promotion` policy and
maps the returned document into the shared PolicyDecision. An undefined result fails closed to a
deny so a missing policy never promotes a change.
"""

from __future__ import annotations

import httpx
from adapters.interfaces.policy_engine import BLOCK_AND_REPORT, PolicyDecision, PolicyInput
from scripts.config import Settings, get_settings

_DECISION_PATH = "/v1/data/agentic/promotion/decision"


def _decision_from_result(result: dict) -> PolicyDecision:
    reasons = tuple(str(r) for r in (result.get("reasons") or ()))
    return PolicyDecision(
        allow=bool(result.get("allow", False)),
        bundle_version=str(result.get("bundle_version") or ""),
        reasons=reasons,
        next_action=str(result.get("next_action") or BLOCK_AND_REPORT),
    )


class RealOpaClient:
    def __init__(
        self,
        *,
        base_url: str,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._transport = transport
        self._timeout = timeout

    @classmethod
    def from_settings(
        cls,
        settings: Settings | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> RealOpaClient:
        resolved = settings or get_settings()
        return cls(base_url=resolved.opa_url, transport=transport)

    async def evaluate(self, decision_input: PolicyInput) -> PolicyDecision:
        async with httpx.AsyncClient(
            base_url=self._base_url, transport=self._transport, timeout=self._timeout
        ) as client:
            resp = await client.post(_DECISION_PATH, json={"input": decision_input.as_document()})
            resp.raise_for_status()
            body = resp.json()
        return _decision_from_result(body.get("result") or {})
