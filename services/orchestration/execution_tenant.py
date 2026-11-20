"""Scope a query of the executions table to the tenant an operator command acts for."""

from __future__ import annotations

from database.models.schema import executions
from sqlalchemy import ColumnElement, or_


def tenant_executions(tenant: str) -> ColumnElement[bool]:
    """Match the executions of ``tenant``, or those recorded without a tenant when it is empty."""
    if tenant:
        return executions.c.tenant == tenant
    return or_(executions.c.tenant.is_(None), executions.c.tenant == "")
