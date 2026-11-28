"""The source of the tokens repository intelligence accepts from each tenant.

In AWS, REPO_INTELLIGENCE_TOKEN_SECRET_ARNS maps each tenant to the ARN of its token secret.
Repository intelligence reads the AWSCURRENT and the AWSPREVIOUS version of each secret with its
task role and accepts both, so a worker that still presents the token it started with keeps its
access after a rotation until the next rotation. A secret without an AWSPREVIOUS version, one
that has never rotated, contributes its current version alone.

The tokens are read again once TENANT_CONFIG_REFRESH_SECONDS have passed since the last read. A
presented token that matches none triggers an early read, at most once every
UNKNOWN_TOKEN_RELOAD_SECONDS, so a worker that restarts with a newly rotated token before the
next scheduled read is accepted. A tenant whose secret fails to read keeps its last accepted
tokens.

Without REPO_INTELLIGENCE_TOKEN_SECRET_ARNS, each tenant's one token comes from
REPO_INTELLIGENCE_TOKEN_<TENANT> or REPO_INTELLIGENCE_TOKEN_KEY, read once at startup.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Protocol

from services.integration.credentials import ConfigError
from services.repository_intelligence.tenant_tokens import AcceptedTokens, single_tokens
from services.repository_intelligence.tenants import configured_tokens, served_tenants

if TYPE_CHECKING:
    from scripts.config import Settings

log = logging.getLogger(__name__)

ARN_MAP_ENV = "REPO_INTELLIGENCE_TOKEN_SECRET_ARNS"
CURRENT_STAGE = "AWSCURRENT"
PREVIOUS_STAGE = "AWSPREVIOUS"
UNKNOWN_TOKEN_RELOAD_SECONDS = 10.0
SECRETS_CONNECT_TIMEOUT_SECONDS = 3
SECRETS_READ_TIMEOUT_SECONDS = 5
SECRETS_MAX_ATTEMPTS = 2
_NOT_FOUND = "ResourceNotFoundException"


class TokenSource(Protocol):
    def accepted(self) -> AcceptedTokens:
        """Return the tokens each tenant may present."""
        ...

    def reload_after_miss(self) -> AcceptedTokens | None:
        """Read the tokens again after a presented token matched none, or return None."""
        ...


class StaticTokens:
    """Tokens fixed for the life of the process."""

    def __init__(self, tokens: AcceptedTokens) -> None:
        self._tokens = tokens

    def accepted(self) -> AcceptedTokens:
        return self._tokens

    def reload_after_miss(self) -> AcceptedTokens | None:
        return None


def secret_arns(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return the token secret ARN of each tenant that REPO_INTELLIGENCE_TOKEN_SECRET_ARNS names."""
    values = os.environ if environ is None else environ
    raw = values.get(ARN_MAP_ENV, "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{ARN_MAP_ENV} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict) or not all(
        isinstance(tenant, str) and isinstance(arn, str) and arn.strip()
        for tenant, arn in parsed.items()
    ):
        raise ConfigError(f"{ARN_MAP_ENV} must map each tenant name to a secret ARN")
    return {tenant: arn.strip() for tenant, arn in parsed.items()}


def _error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if not isinstance(response, Mapping):
        return ""
    error = response.get("Error")
    return str(error.get("Code", "")) if isinstance(error, Mapping) else ""


class SecretTokens:
    """The current and previous token of each tenant, read from Secrets Manager."""

    def __init__(
        self,
        client: Any,
        arns: Mapping[str, str],
        *,
        interval: float,
        miss_interval: float = UNKNOWN_TOKEN_RELOAD_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._arns = dict(arns)
        self._interval = interval
        self._miss_interval = miss_interval
        self._clock = clock
        self._tokens: dict[str, tuple[str, ...]] = {}
        self._read_at: float | None = None
        self._lock = threading.Lock()

    def accepted(self) -> AcceptedTokens:
        """Return the accepted tokens, read again once the refresh interval has passed."""
        self._read_if_older_than(self._interval)
        return self._tokens

    def reload_after_miss(self) -> AcceptedTokens | None:
        """Read the tokens again unless the last read is more recent than the miss interval."""
        if not self._read_if_older_than(self._miss_interval):
            return None
        return self._tokens

    def _read_if_older_than(self, seconds: float) -> bool:
        with self._lock:
            now = self._clock()
            if self._read_at is not None and now - self._read_at < seconds:
                return False
            self._read_at = now
            tokens = dict(self._tokens)
            for tenant, arn in self._arns.items():
                read = self._read_tenant(tenant, arn)
                if read:
                    tokens[tenant] = read
            self._tokens = tokens
            return True

    def _read_tenant(self, tenant: str, arn: str) -> tuple[str, ...]:
        try:
            current = self._value(arn, CURRENT_STAGE)
        except Exception as exc:  # noqa: BLE001
            log.error(
                "cannot read the AWSCURRENT stage of %s for tenant %s, keeping its last accepted "
                "values: %s",
                arn,
                tenant,
                exc,
            )
            return ()
        if not current:
            log.error("the AWSCURRENT stage of %s for tenant %s is empty", arn, tenant)
            return ()
        try:
            previous = self._value(arn, PREVIOUS_STAGE)
        except Exception as exc:  # noqa: BLE001
            if _error_code(exc) == _NOT_FOUND:
                return (current,)
            log.error(
                "cannot read the AWSPREVIOUS stage of %s for tenant %s, keeping its last accepted "
                "values beside AWSCURRENT: %s",
                arn,
                tenant,
                exc,
            )
            kept = [token for token in self._tokens.get(tenant, ()) if token != current]
            return (current, *kept)
        if previous and previous != current:
            return (current, previous)
        return (current,)

    def _value(self, arn: str, stage: str) -> str:
        response = self._client.get_secret_value(SecretId=arn, VersionStage=stage)
        return str(response.get("SecretString") or "").strip()


def secrets_manager_client(settings: Settings) -> Any:
    """Build a Secrets Manager client on the default credential chain, the task role in AWS.

    The tokens are read on the request path under one lock, so the client gives up within
    seconds on an endpoint it cannot reach, and the read keeps the last accepted tokens, rather
    than holding every request and health check for the SDK's default minute per attempt.
    """
    import boto3
    from botocore.config import Config
    from scripts.aws_clients import configure_aws_fips

    configure_aws_fips(settings)
    return boto3.client(
        "secretsmanager",
        region_name=settings.aws_region,
        config=Config(
            connect_timeout=SECRETS_CONNECT_TIMEOUT_SECONDS,
            read_timeout=SECRETS_READ_TIMEOUT_SECONDS,
            retries={"max_attempts": SECRETS_MAX_ATTEMPTS, "mode": "standard"},
        ),
    )


def token_source(
    settings: Settings,
    environ: Mapping[str, str] | None = None,
    *,
    client_factory: Callable[[Settings], Any] = secrets_manager_client,
) -> TokenSource:
    """Return the token source the environment configures, Secrets Manager or static tokens."""
    arns = secret_arns(environ)
    tenants = served_tenants(settings)
    if not arns or not tenants:
        return StaticTokens(single_tokens(configured_tokens(settings, environ)))
    missing = sorted(set(tenants) - set(arns))
    if missing:
        log.error(
            "%s maps no ARN for tenants %s, so their requests are refused",
            ARN_MAP_ENV,
            ", ".join(missing),
        )
    served = {tenant: arn for tenant, arn in arns.items() if tenant in tenants}
    return SecretTokens(
        client_factory(settings), served, interval=float(settings.tenant_config_refresh_seconds)
    )
