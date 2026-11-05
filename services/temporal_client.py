"""The one path every platform client takes to connect to Temporal.

TLS is on when `TEMPORAL_TLS_CA` or `TEMPORAL_TLS_SERVER_NAME` is set and off when both are
empty, which is the local stack. `TEMPORAL_TLS_CA` holds the PEM bundle that issued the
frontend certificate, either inline, as ECS injects it from Secrets Manager, or as a file path.
With only a server name, the system trust store verifies the frontend, as for a public ACM
certificate. The server name is the name the frontend certificate carries, which differs from
the NLB address the client dials.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from scripts.config import Settings
from temporalio.client import Client
from temporalio.service import TLSConfig

_PEM_PREFIX = "-----BEGIN"


def tls_config(settings: Settings) -> TLSConfig | bool:
    ca = settings.temporal_tls_ca.strip()
    server_name = settings.temporal_tls_server_name.strip()
    if not ca and not server_name:
        return False
    return TLSConfig(server_root_ca_cert=_ca_bundle(ca) if ca else None, domain=server_name or None)


def _ca_bundle(value: str) -> bytes:
    if value.startswith(_PEM_PREFIX):
        return value.encode()
    path = Path(value).expanduser()
    if not path.is_file():
        raise ValueError(
            f"TEMPORAL_TLS_CA names {path}, which is not a file. Set it to the PEM bundle or to "
            "the path of a file holding it."
        )
    return path.read_bytes()


async def connect(target_host: str, settings: Settings, **options: Any) -> Client:
    """Connect to `target_host` in the configured namespace, over TLS when it is configured."""
    return await Client.connect(
        target_host,
        namespace=settings.temporal_namespace,
        tls=tls_config(settings),
        **options,
    )
