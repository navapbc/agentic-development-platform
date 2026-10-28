"""The single renderer for operator-facing messages that carry a remedy.

Every surface that tells an operator a run stopped renders the remedy through ``with_remedy`` so
the ``Next step:`` line reads the same in a Slack alert, a CLI error, and an audit record.
"""

from __future__ import annotations

NO_NEXT_STEP = "__no_next_step__"
"""Supplied as ``remedy`` where the message reports an outcome an operator cannot act on."""

NEXT_STEP_PREFIX = "Next step:"


class RemediableError(Exception):
    """An error whose message states what happened and whose ``remedy`` states what to do.

    ``str(exc)`` carries only the condition so callers can log, match, and wrap it without
    operator instructions bleeding into other contexts. Operator-facing surfaces render
    ``operator_message()`` instead.
    """

    remedy: str = ""

    def operator_message(self) -> str:
        return with_remedy(str(self), self.remedy)


def with_remedy(body: str, remedy: str) -> str:
    """Append the remedy to ``body`` as a ``Next step:`` line, or return ``body`` unchanged."""
    if not remedy or remedy == NO_NEXT_STEP:
        return body
    return f"{body}\n{NEXT_STEP_PREFIX} {remedy}"


def operator_text(exc: BaseException) -> str:
    """Render any exception for an operator, adding its remedy when it carries one."""
    if isinstance(exc, RemediableError):
        return exc.operator_message()
    return str(exc)
