"""Join model generations to the execution session at the LiteLLM gateway.

The LiteLLM proxy carries a global Langfuse callback that reads join keys from
request headers prefixed ``langfuse_``. The planning path sets those keys through
the request body, but the coding agent and the rebar-run review and repair calls
build their own HTTP clients with no metadata seam. This module derives the join
headers from the ambient execution baggage and installs an ``httpx`` transport
that adds them to every request bound for the proxy, so those generations group
under the execution session rather than landing orphaned.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from urllib.parse import urlparse

from observability import attributes as attrs
from observability.context import current_identifiers

log = logging.getLogger("observability")

SESSION_HEADER = "langfuse_session_id"
GENERATION_NAME_HEADER = "langfuse_generation_name"
VERSION_HEADER = "langfuse_version"
TRACE_ID_HEADER = "langfuse_trace_id"

_HEADER_MAP: tuple[tuple[str, str], ...] = (
    (attrs.EXECUTION_ID, SESSION_HEADER),
    (attrs.STAGE, GENERATION_NAME_HEADER),
    (attrs.PROMPT_VERSION, VERSION_HEADER),
)


def langfuse_headers_from_identifiers(identifiers: Mapping[str, str]) -> dict[str, str]:
    """Build the ``langfuse_`` request headers from the platform identifiers."""
    headers: dict[str, str] = {}
    for key, header in _HEADER_MAP:
        value = identifiers.get(key)
        if value:
            headers[header] = str(value)
    return headers


def current_trace_id() -> str:
    """Return the active OpenTelemetry trace id as 32 hex characters, or empty."""
    try:
        from opentelemetry import trace

        context = trace.get_current_span().get_span_context()
        if context.is_valid:
            return format(context.trace_id, "032x")
    except Exception:  # noqa: BLE001 - trace lookup must not break a request
        pass
    return ""


def current_langfuse_headers() -> dict[str, str]:
    """Build the join headers from the current execution baggage and trace."""
    headers = langfuse_headers_from_identifiers(current_identifiers())
    trace_id = current_trace_id()
    if trace_id:
        headers[TRACE_ID_HEADER] = trace_id
    return headers


_installed = False


def install_litellm_header_propagation(base_url: str | None) -> bool:
    """Add the join headers to every httpx request bound for the LiteLLM proxy.

    Swaps ``httpx.AsyncHTTPTransport`` for a subclass that injects the ambient
    join headers on requests whose host matches the proxy, reaching clients that
    expose no metadata seam. Idempotent, and a no-op without a base url.
    """
    global _installed
    if _installed or not base_url:
        return False

    import httpx

    parsed = urlparse(base_url)
    target_host = parsed.hostname
    target_port = parsed.port
    base_transport = httpx.AsyncHTTPTransport

    class _HeaderInjectingAsyncHTTPTransport(base_transport):
        async def handle_async_request(self, request):  # type: ignore[override]
            try:
                url = request.url
                host_match = url.host == target_host
                port_match = target_port is None or url.port == target_port
                if host_match and port_match:
                    headers = current_langfuse_headers()
                    if _is_completion_path(url.path) and SESSION_HEADER not in headers:
                        log.error(
                            "litellm generation join failed: a model call to %s carried no "
                            "session header from the execution baggage, the generation will "
                            "not group under an execution in Langfuse",
                            url.path,
                        )
                    for header, value in headers.items():
                        if header not in request.headers:
                            request.headers[header] = value
            except Exception:  # noqa: BLE001 - header enrichment must not break a request
                pass
            return await super().handle_async_request(request)

    httpx.AsyncHTTPTransport = _HeaderInjectingAsyncHTTPTransport
    _installed = True
    return True


def _is_completion_path(path: str) -> bool:
    """Return whether the request path is a model completion call worth joining."""
    return "completions" in path or "/messages" in path


def reset_header_propagation_for_testing() -> None:
    global _installed
    _installed = False
