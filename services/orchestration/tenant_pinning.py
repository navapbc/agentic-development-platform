"""Pin each run's activities to the tenant config bundle its manifest recorded.

The first activity of a run, record_manifest, records the identity of the bundle active when it
runs. This interceptor reads that identity once per run and has every later activity of the run
read that bundle's copy, so a bundle published while the run is in flight changes nothing the
run reads. An activity of a run that has not recorded its manifest, such as intake, reads the
bundle active when it starts, for its whole duration.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from temporalio.exceptions import ApplicationError
from temporalio.worker import ActivityInboundInterceptor, ExecuteActivityInput
from temporalio.worker import Interceptor as WorkerInterceptor

from services.integration.credentials import ConfigError
from services.integration.tenant_config import (
    BundleIdentity,
    BundleUnavailableError,
    TenantConfig,
    configure,
    pinned,
)

log = logging.getLogger(__name__)

Lookup = Callable[[str], Awaitable[tuple[str, dict[str, Any]] | None]]
_CACHE_SIZE = 512
TENANT_MISMATCH = "TenantMismatch"


def _execution_id(args: Sequence[Any]) -> str:
    for arg in args:
        value = (
            arg.get("execution_id") if isinstance(arg, dict) else getattr(arg, "execution_id", "")
        )
        if value:
            return str(value)
    return ""


async def _recorded_bundle(execution_id: str) -> tuple[str, dict[str, Any]] | None:
    from services.coordination.manifest import recorded_config_bundle

    return await recorded_config_bundle(execution_id, _engine())


_shared_engine: Any = None


def _engine() -> Any:
    global _shared_engine
    if _shared_engine is None:
        from database.engine import create_platform_engine
        from scripts.config import get_settings

        _shared_engine = create_platform_engine(get_settings().database_url)
    return _shared_engine


class TenantConfigInterceptor(WorkerInterceptor):
    def __init__(self, config: TenantConfig, lookup: Lookup = _recorded_bundle) -> None:
        self.config = config
        self._lookup = lookup
        self._pins: OrderedDict[str, BundleIdentity] = OrderedDict()

    def intercept_activity(self, next: ActivityInboundInterceptor) -> ActivityInboundInterceptor:
        return _PinnedActivityInbound(next, self)

    async def pinned_identity(self, execution_id: str) -> BundleIdentity | None:
        if not execution_id:
            return None
        if execution_id in self._pins:
            self._pins.move_to_end(execution_id)
            return self._pins[execution_id]
        recorded = await self._lookup(execution_id)
        if recorded is None:
            return None
        tenant, raw = recorded
        identity = BundleIdentity.from_manifest(tenant, raw)
        self._pins[execution_id] = identity
        while len(self._pins) > _CACHE_SIZE:
            self._pins.popitem(last=False)
        return identity


class _PinnedActivityInbound(ActivityInboundInterceptor):
    def __init__(self, next: ActivityInboundInterceptor, owner: TenantConfigInterceptor) -> None:
        super().__init__(next)
        self._owner = owner

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        config = self._owner.config
        identity = await self._owner.pinned_identity(_execution_id(input.args))
        if identity is None:
            identity, path = config.active, config.active_path
        else:
            try:
                path = await asyncio.to_thread(config.path_for, identity)
            except BundleUnavailableError:
                raise
            except ConfigError as exc:
                raise ApplicationError(str(exc), type=TENANT_MISMATCH, non_retryable=True) from exc
        with pinned(path, identity):
            return await super().execute_activity(input)


def _alert_rejected(identity: BundleIdentity, reason: str) -> None:
    from services.orchestration.notifications import AlertMessage, deliver_alert

    deliver_alert(
        AlertMessage(
            subject=f"Config bundle {identity.id} of tenant {identity.tenant} was refused",
            body=(
                f"The workers refused config bundle {identity.id} of tenant {identity.tenant} "
                f"and keep the last valid bundle active.\n{reason}"
            ),
            issue_key="",
        )
    )


def start_tenant_config(settings: Any) -> TenantConfig:
    """Load the worker's tenant config, refusing to start without a tenant or a valid bundle."""
    if not settings.tenant:
        raise ConfigError(
            "TENANT is not set. Every activity worker serves one tenant, and there is no default."
        )
    return configure(settings, on_rejected=_alert_rejected)


async def refresh_tenant_config(config: TenantConfig, interval_seconds: float) -> None:
    """Check the tenant's source for a new bundle every interval, forever."""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await asyncio.to_thread(config.refresh)
        except Exception:  # noqa: BLE001 - a failed check keeps the active bundle
            log.warning(
                "could not refresh the config bundle of tenant %s", config.tenant, exc_info=True
            )
