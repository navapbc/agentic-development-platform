"""Activity for the durable counter demo: one tick is one second of work."""

from __future__ import annotations

import asyncio

from temporalio import activity

TICK_SECONDS = 1.0


@activity.defn
async def count_tick(n: int) -> int:
    """Sleep one tick and echo the number.

    The counter workflow runs one of these per number so the count advances on
    the worker. Killing the worker interrupts a tick and Temporal retries it on
    a fresh worker, rather than the count being an in-process client loop.
    """
    await asyncio.sleep(TICK_SECONDS)
    return n
