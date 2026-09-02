"""OpenTelemetry log export setup.

Attaches an OTLP logging handler to the root logger so platform log records are
exported to the collector logs pipeline alongside the traces and metrics. Every
entry point is best effort and never raises into a caller.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from opentelemetry.sdk.resources import Resource

log = logging.getLogger("observability")

_configured = False
_provider: Any | None = None
_handler: logging.Handler | None = None


def _build_log_exporter() -> Any | None:
    try:
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter

        return OTLPLogExporter()
    except Exception:  # noqa: BLE001
        log.warning("otlp log exporter unavailable; logs will not be exported", exc_info=True)
        return None


def configure_logging(
    service_name: str, *, exporter: Any | None = None, level: int = logging.INFO
) -> bool:
    """Attach an OTLP logging handler to the root logger once. Returns True when active."""
    global _configured, _provider, _handler
    if _configured:
        return True
    try:
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

        log_exporter = exporter if exporter is not None else _build_log_exporter()
        if log_exporter is None:
            return False
        provider = LoggerProvider(resource=Resource.create({"service.name": service_name}))
        provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
        handler = LoggingHandler(level=level, logger_provider=provider)
        logging.getLogger().addHandler(handler)
        _provider = provider
        _handler = handler
        _configured = True
        return True
    except Exception:  # noqa: BLE001
        log.warning("log export setup failed; continuing without log export", exc_info=True)
        return False


def force_flush_logs(timeout_millis: int = 5000) -> None:
    """Flush pending log records through the configured provider. Never raises."""
    with contextlib.suppress(Exception):
        if _provider is not None:
            _provider.force_flush(timeout_millis)


def reset_logging_for_testing() -> None:
    """Detach the handler and clear the one-time guard. For tests only."""
    global _configured, _provider, _handler
    if _handler is not None:
        logging.getLogger().removeHandler(_handler)
    _configured = False
    _provider = None
    _handler = None
