"""Coordination-service entrypoint: health plus idempotency, sequencing, and claim RPC."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from scripts.config import get_settings
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from services.coordination.claims import (
    PostgresWorkClaimStore,
    WorkClaimStore,
    WorkUnit,
    partition_by_file_impact,
)
from services.coordination.idempotency import (
    PostgresSideEffectLedger,
    SideEffectLedger,
    SideEffectRef,
)
from services.coordination.sequencing import (
    PostgresSequenceGate,
    SequencedAction,
    SequenceGate,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("coordination-service")

PORT = 8130


async def _ping(database_url: str) -> None:
    engine = create_async_engine(database_url, future=True)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    finally:
        await engine.dispose()


def _agent_platform_healthy(loop: asyncio.AbstractEventLoop) -> bool:
    try:
        asyncio.run_coroutine_threadsafe(_ping(get_settings().database_url), loop).result()
        return True
    except Exception:
        log.exception("agent_platform ping failed")
        return False


def build_handler(
    ledger: SideEffectLedger,
    gate: SequenceGate,
    claim_store: WorkClaimStore,
    loop: asyncio.AbstractEventLoop,
) -> type[BaseHTTPRequestHandler]:
    def _run(coro: Coroutine[Any, Any, Any]) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, loop).result()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path in ("/health", "/healthz", "/"):
                healthy = _agent_platform_healthy(loop)
                self._respond(
                    200 if healthy else 503,
                    {"status": "ok" if healthy else "degraded", "service": "coordination"},
                )
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self) -> None:
            try:
                payload = self._read_json()
            except ValueError:
                self._respond(400, {"error": "invalid json body"})
                return
            try:
                result = self._dispatch(self.path, payload)
            except KeyError:
                self.send_response(404)
                self.end_headers()
                return
            except (TypeError, ValueError) as exc:
                self._respond(400, {"error": str(exc)})
                return
            self._respond(200, result)

        def _dispatch(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
            if path == "/idempotency/reserve":
                ref = SideEffectRef(**payload)
                return asdict(_run(ledger.reserve(ref)))
            if path == "/idempotency/record":
                result = payload.pop("result")
                ref = SideEffectRef(**payload)
                return asdict(_run(ledger.record(ref, result)))
            if path == "/sequence/admit":
                action = SequencedAction(**payload)
                return asdict(_run(gate.admit(action)))
            if path == "/claims/try":
                unit = _work_unit(payload)
                return asdict(_run(claim_store.try_claim(unit)))
            if path == "/claims/next_batch":
                units = [_work_unit(u) for u in payload["units"]]
                schedule = partition_by_file_impact(units)
                return {
                    "admitted": [asdict(u) for u in schedule.admitted],
                    "deferred": [asdict(u) for u in schedule.deferred],
                }
            raise KeyError(path)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b""
            try:
                return json.loads(raw or b"{}")
            except json.JSONDecodeError as exc:
                raise ValueError(str(exc)) from exc

        def _respond(self, code: int, body: dict[str, Any]) -> None:
            encoded = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *args) -> None:
            return

    return Handler


def _work_unit(payload: dict[str, Any]) -> WorkUnit:
    data = dict(payload)
    data["file_impact"] = tuple(data.get("file_impact") or ())
    return WorkUnit(**data)


def _temporal_owner_active() -> Callable[[str], Awaitable[bool]]:
    settings = get_settings()
    cache: dict[str, Any] = {"client": None}
    lock = asyncio.Lock()

    async def _client() -> Any:
        from temporalio.client import Client

        if cache["client"] is None:
            async with lock:
                if cache["client"] is None:
                    cache["client"] = await Client.connect(
                        settings.temporal_host, namespace=settings.temporal_namespace
                    )
        return cache["client"]

    async def _active(workflow_id: str) -> bool:
        from temporalio.client import WorkflowExecutionStatus
        from temporalio.service import RPCError, RPCStatusCode

        try:
            client = await _client()
            description = await client.get_workflow_handle(workflow_id).describe()
        except RPCError as exc:
            return exc.status != RPCStatusCode.NOT_FOUND
        except Exception:  # noqa: BLE001
            cache["client"] = None
            return True
        return description.status == WorkflowExecutionStatus.RUNNING

    return _active


def main() -> None:
    from database.schema_guard import wait_for_platform_schema

    settings = get_settings()
    asyncio.run(wait_for_platform_schema(settings.database_url))
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, name="coordination-loop", daemon=True).start()
    handler = build_handler(
        PostgresSideEffectLedger(),
        PostgresSequenceGate(),
        PostgresWorkClaimStore(owner_active=_temporal_owner_active()),
        loop,
    )
    log.info("coordination-service listening on %s:%d", settings.bind_host, PORT)
    ThreadingHTTPServer((settings.bind_host, PORT), handler).serve_forever()


if __name__ == "__main__":
    main()
