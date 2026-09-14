"""OpenTelemetry log export setup.

Attaches an OTLP logging handler to the root logger so platform log records are
exported to the collector logs pipeline alongside the traces and metrics. Every
entry point is best effort and never raises into a caller. A redaction filter
scrubs AWS credential material from every record before it is exported or
printed, so a worker that holds Bedrock credentials never leaks them to logs.
"""

from __future__ import annotations

import contextlib
import logging
import re
from typing import Any

from opentelemetry.sdk.resources import Resource

log = logging.getLogger("observability")

_configured = False
_provider: Any | None = None
_handler: logging.Handler | None = None
_redaction_installed = False

_PLACEHOLDER = "[redacted]"
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ASIA[0-9A-Z]{16}"),
    re.compile(r"(?i)(aws_secret_access_key|aws_session_token|aws_access_key_id)\s*[:=]\s*\S+"),
    re.compile(r"(?i)(secret_access_key|session_token|access_key_id)\s*[\"']?\s*[:=]\s*\S+"),
)


def redact_secrets(text: str) -> str:
    """Replace AWS credential material in a string with a placeholder."""
    redacted = text
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(_PLACEHOLDER, redacted)
    return redacted


class _SecretRedactingFilter(logging.Filter):
    """Scrub AWS credential material from a record's rendered message."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - never drop a record over a formatting error
            return True
        redacted = redact_secrets(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def install_log_redaction() -> None:
    """Attach the secret-redaction filter to the root logger's handlers. Never raises."""
    global _redaction_installed
    with contextlib.suppress(Exception):
        root = logging.getLogger()
        redactor = _SecretRedactingFilter()
        for handler in root.handlers:
            if not any(isinstance(f, _SecretRedactingFilter) for f in handler.filters):
                handler.addFilter(redactor)
        _redaction_installed = True


def _redact_handler(handler: logging.Handler) -> None:
    with contextlib.suppress(Exception):
        if not any(isinstance(f, _SecretRedactingFilter) for f in handler.filters):
            handler.addFilter(_SecretRedactingFilter())


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
    install_log_redaction()
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
        _redact_handler(handler)
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
    global _configured, _provider, _handler, _redaction_installed
    if _handler is not None:
        logging.getLogger().removeHandler(_handler)
    _configured = False
    _provider = None
    _handler = None
    _redaction_installed = False
