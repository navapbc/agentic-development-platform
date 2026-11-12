"""AWS SDK FIPS configuration bridge.

botocore selects FIPS service endpoints when the ``AWS_USE_FIPS_ENDPOINT`` environment
variable is set. This bridges the platform's ``aws_use_fips_endpoint`` setting to that
variable so every boto3 client built in the process, the native review Bedrock client,
the evidence object-store client, and the rebar ticket-store sync, resolves FIPS
endpoints without per-client wiring. Services that are not this process, such as the
LiteLLM gateway, receive the variable through their container environment.
"""

from __future__ import annotations

import os
from typing import Any

from scripts.config import Settings, get_settings

_FIPS_ENV_VAR = "AWS_USE_FIPS_ENDPOINT"


def configure_aws_fips(settings: Settings | None = None) -> None:
    """Export ``AWS_USE_FIPS_ENDPOINT`` when the platform setting enables FIPS.

    A no-op when the setting is off, so a local environment that reaches MinIO and the
    tunnel on custom endpoints is unaffected. It never unsets a value already present in
    the environment, so an explicit container variable stays authoritative.
    """
    resolved = settings or get_settings()
    if resolved.aws_use_fips_endpoint and _FIPS_ENV_VAR not in os.environ:
        os.environ[_FIPS_ENV_VAR] = "true"


def s3_client(
    *,
    region: str,
    endpoint_url: str = "",
    access_key: str = "",
    secret_key: str = "",
) -> Any:
    """Build an S3 client, signing with the given keys when both are set.

    A client given explicit keys ignores ``AWS_PROFILE``. Compose passes the operator profile
    from ``.env`` into containers that do not mount ``~/.aws``, and botocore raises
    ProfileNotFound for a missing profile even when the client is given keys. Without keys
    the default credential chain applies, which is the task role in AWS.
    """
    import boto3
    import botocore.session

    kwargs: dict[str, Any] = {"region_name": region}
    if endpoint_url:
        kwargs["endpoint_url"] = endpoint_url
    if not (access_key and secret_key):
        return boto3.client("s3", **kwargs)
    profile_free = botocore.session.Session(session_vars={"profile": (None, None, None, None)})
    return boto3.Session(botocore_session=profile_free).client(
        "s3", aws_access_key_id=access_key, aws_secret_access_key=secret_key, **kwargs
    )
