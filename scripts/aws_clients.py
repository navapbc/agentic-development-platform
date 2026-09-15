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
