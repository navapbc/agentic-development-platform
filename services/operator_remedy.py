"""The single renderer for operator-facing messages that carry a remedy.

Every surface that tells an operator a run stopped renders the remedy through ``with_remedy`` so
the ``Next step:`` line reads the same in a Slack alert, a CLI error, and an audit record.
"""

from __future__ import annotations

NO_NEXT_STEP = "__no_next_step__"
"""Supplied as ``remedy`` where the message reports an outcome an operator cannot act on."""

NEXT_STEP_PREFIX = "Next step:"


class RemediableError(Exception):
    """An error that states what happened and, separately, what the operator should do.

    ``str(exc)`` renders both, because the surfaces that show an operator an exception mostly do
    so through ``str``, including the Temporal failure converter and every ``%s`` log call.
    ``remedy`` stays separately addressable so audit records and alerts can store the guidance as
    its own field rather than parsing it back out of a message.
    """

    remedy: str = ""

    def condition(self) -> str:
        """What happened, with no instruction to the operator."""
        return super().__str__()

    def __str__(self) -> str:
        return with_remedy(self.condition(), self.remedy)


def with_remedy(body: str, remedy: str) -> str:
    """Append the remedy to ``body`` as a ``Next step:`` line, or return ``body`` unchanged."""
    if not remedy or remedy == NO_NEXT_STEP:
        return body
    return f"{body}\n{NEXT_STEP_PREFIX} {remedy}"
