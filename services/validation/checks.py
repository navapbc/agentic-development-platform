"""The check registry, which holds each check's scope, its name, and the coroutine that runs it."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace

from services.operator_remedy import remedy
from services.validation.report import CheckResult, failed

log = logging.getLogger(__name__)

CONFIRM_AGAIN = "Validate again, and this check passes."


@dataclass(frozen=True)
class Check[Context]:
    """A check of one scope. ``dependency`` names the platform dependency its results fold into."""

    name: str
    scope: str
    run: Callable[[Context], Awaitable[list[CheckResult]]]
    dependency: str = ""


async def _run_one[Context](check: Check[Context], context: Context) -> list[CheckResult]:
    try:
        results = await check.run(context)
    except Exception as exc:  # noqa: BLE001
        log.warning("the %s check raised", check.name, exc_info=True)
        results = [
            failed(
                check.name,
                f"the check raised {type(exc).__name__}. {exc}",
                remedy("Read the worker log for the traceback, then correct its cause."),
                confirm=CONFIRM_AGAIN,
            )
        ]
    return [
        replace(result, scope=check.scope, dependency=check.dependency or result.dependency)
        for result in results
    ]


async def run_checks[Context](
    checks: Sequence[Check[Context]], context: Context
) -> list[CheckResult]:
    """Run every check concurrently, so one slow or raising check delays or fails only itself."""
    batches = await asyncio.gather(*(_run_one(check, context) for check in checks))
    return [result for batch in batches for result in batch]
