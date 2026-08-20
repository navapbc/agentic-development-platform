"""Ambient platform identifiers carried in OpenTelemetry baggage.

Baggage travels with the OpenTelemetry context, so identifiers bound here are
available to child spans in the same process and, when the tracing propagator
carries baggage, across the Temporal workflow and activity boundary.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

from opentelemetry import baggage
from opentelemetry import context as otel_context

from observability.attributes import PLATFORM_KEYS


def current_identifiers() -> dict[str, str]:
    """Return the platform identifiers present in the current baggage."""
    values: dict[str, str] = {}
    for key in PLATFORM_KEYS:
        value = baggage.get_baggage(key)
        if value is not None:
            values[key] = str(value)
    return values


@contextlib.contextmanager
def bind_execution(**identifiers: str | None) -> Iterator[None]:
    """Attach platform identifiers to baggage for the duration of the block.

    Unknown keys and ``None`` values are ignored. The previous context is
    restored on exit. Failures never propagate to the caller.
    """
    token = None
    try:
        ctx = otel_context.get_current()
        changed = False
        for key, value in identifiers.items():
            if value is None or key not in PLATFORM_KEYS:
                continue
            ctx = baggage.set_baggage(key, str(value), context=ctx)
            changed = True
        if changed:
            token = otel_context.attach(ctx)
        yield
    finally:
        if token is not None:
            otel_context.detach(token)
