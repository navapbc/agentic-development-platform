"""Write the workstation's AWS credentials to the cluster Secret the Bedrock clients read.

LiteLLM and each tenant's review worker read the Secret through credential_process, which rereads
it before the credentials it last returned expire, so a renewal reaches them without a restart.
The Secret is written to the platform namespace and to each local tenant's review namespace.

- AWS_LOGIN_COMMAND, when set, is run to sign in and must print credentials in the
  credential_process JSON format, for example `kion favorite <name> --credential-process`.
  It runs attached to the terminal, so a browser or Okta sign-in completes interactively.
- Otherwise the profile in PLATFORM_AWS_PROFILE or AWS_PROFILE is resolved through the AWS SDK,
  running `aws sso login` first when its SSO session has expired.
- A static access key, one with no session token, is exchanged for session credentials through
  sts:GetSessionToken for AWS_LOGIN_SESSION_SECONDS, unless AWS_LOGIN_STATIC_PASSTHROUGH is set.

The credentials are piped to kubectl and never written to disk.
"""

from __future__ import annotations

import base64
import json
import os
import shlex
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from services import process
from services.aws_credentials import expiration_stamp, expiry_report

from scripts.config import Settings, get_settings

LOG = "[aws-login]"
CREDENTIALS_OBJECT = "aws-credentials"
KEY = "credentials.json"


class LoginError(Exception):
    """Credentials could not be obtained for the configured profile."""


def profile_name(settings: Settings, environ: Mapping[str, str] = os.environ) -> str:
    return environ.get("PLATFORM_AWS_PROFILE") or settings.aws_profile


def parse_process_output(text: str) -> dict[str, Any]:
    try:
        record = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LoginError(f"AWS_LOGIN_COMMAND printed no credential_process JSON: {exc}") from exc
    if not (record.get("AccessKeyId") and record.get("SecretAccessKey")):
        raise LoginError("AWS_LOGIN_COMMAND printed JSON without AccessKeyId and SecretAccessKey")
    return {key: value for key, value in record.items() if value}


def from_command(command: str, run: Callable[..., Any] = process.run) -> dict[str, Any]:
    result = run(
        shlex.split(command),
        timeout=process.LONG_COMMAND_TIMEOUT_SECONDS,
        stdout=process.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise LoginError(f"AWS_LOGIN_COMMAND exited {result.returncode}")
    return parse_process_output(result.stdout)


def from_profile(profile: str, session_factory: Callable[[str], Any] | None = None) -> dict:
    """Resolve the profile's credentials, signing in to an expired SSO session first."""
    from botocore.exceptions import (
        SSOTokenLoadError,
        TokenRetrievalError,
        UnauthorizedSSOTokenError,
    )

    factory = session_factory or _session

    def resolve() -> dict[str, Any]:
        credentials = factory(profile).get_credentials()
        if credentials is None:
            raise LoginError(f"the AWS profile {profile!r} resolves no credentials")
        frozen = credentials.get_frozen_credentials()
        record: dict[str, Any] = {
            "AccessKeyId": frozen.access_key,
            "SecretAccessKey": frozen.secret_key,
        }
        if frozen.token:
            record["SessionToken"] = frozen.token
        expiry = getattr(credentials, "_expiry_time", None)
        if expiry is not None:
            record["Expiration"] = expiration_stamp(expiry)
        return record

    try:
        return resolve()
    except SSOTokenLoadError, TokenRetrievalError, UnauthorizedSSOTokenError:
        print(f"{LOG} the SSO session of {profile} has expired, signing in", file=sys.stderr)
        process.run(
            ["aws", "sso", "login", "--profile", profile],
            timeout=process.LONG_COMMAND_TIMEOUT_SECONDS,
            check=True,
        )
        return resolve()


def _session(profile: str) -> Any:
    import boto3

    return boto3.Session(profile_name=profile)


def session_token(
    record: Mapping[str, Any], seconds: int, region: str, client_factory: Callable | None = None
) -> dict[str, Any]:
    """Exchange a static access key for session credentials that expire after seconds."""
    factory = client_factory or _sts_client
    response = factory(record, region).get_session_token(DurationSeconds=seconds)
    issued = response["Credentials"]
    return {
        "AccessKeyId": issued["AccessKeyId"],
        "SecretAccessKey": issued["SecretAccessKey"],
        "SessionToken": issued["SessionToken"],
        "Expiration": expiration_stamp(issued["Expiration"]),
    }


def _sts_client(record: Mapping[str, Any], region: str) -> Any:
    import boto3

    return boto3.client(
        "sts",
        region_name=region,
        aws_access_key_id=record["AccessKeyId"],
        aws_secret_access_key=record["SecretAccessKey"],
    )


def obtain(
    settings: Settings,
    *,
    environ: Mapping[str, str] = os.environ,
    command_runner: Callable[..., Any] = process.run,
    session_factory: Callable[[str], Any] | None = None,
    client_factory: Callable | None = None,
) -> dict[str, Any]:
    if settings.aws_login_command:
        record = from_command(settings.aws_login_command, command_runner)
    else:
        record = from_profile(profile_name(settings, environ), session_factory)
    if "SessionToken" not in record and not settings.aws_login_static_passthrough:
        record = session_token(
            record, settings.aws_login_session_seconds, settings.aws_region, client_factory
        )
    return {"Version": 1, **record}


def secret_manifest(
    settings: Settings, record: Mapping[str, Any], namespace: str = ""
) -> dict[str, Any]:
    payload = base64.b64encode(json.dumps(record).encode()).decode()
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "type": "Opaque",
        "metadata": {
            "name": CREDENTIALS_OBJECT,
            "namespace": namespace or settings.k8s_namespace,
            "labels": {"app.kubernetes.io/part-of": "agentic-dev-platform"},
        },
        "data": {KEY: payload},
    }


def credential_namespaces(settings: Settings) -> list[str]:
    """Return the platform namespace and the review namespace of each local tenant."""
    from scripts.k8s_cluster import tenants
    from scripts.tenant_render import review_namespace

    return [settings.k8s_namespace, *(review_namespace(name) for name in tenants())]


def login_environ() -> dict[str, str]:
    """Return the process environment over the non-empty values of config/local/platform.env."""
    from scripts.env_file import platform_env_values

    dotenv = {key: value for key, value in platform_env_values().items() if value}
    return {**dotenv, **os.environ}


def stored_record(settings: Settings) -> dict[str, Any] | None:
    """Return the credentials in the platform namespace's Secret, or None when it is absent."""
    from scripts.k8s_cluster import namespaced

    result = process.run(
        namespaced(
            settings,
            "get",
            "secret",
            CREDENTIALS_OBJECT,
            "--ignore-not-found",
            "-o",
            "jsonpath={.data.credentials\\.json}",
        ),
        timeout=process.SHORT_COMMAND_TIMEOUT_SECONDS,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise LoginError(result.stderr.strip() or f"kubectl exited {result.returncode}")
    encoded = result.stdout.strip()
    return json.loads(base64.b64decode(encoded)) if encoded else None


def main(argv: list[str] | None = None) -> int:
    import argparse

    from scripts.k8s_cluster import kubectl

    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    settings = get_settings()
    try:
        record = obtain(settings, environ=login_environ())
    except LoginError as exc:
        print(f"{LOG} {exc}", file=sys.stderr)
        return 1
    namespaces = credential_namespaces(settings)
    manifests = [secret_manifest(settings, record, namespace) for namespace in namespaces]
    process.run(
        kubectl(settings, "apply", "-f", "-"),
        input=json.dumps({"apiVersion": "v1", "kind": "List", "items": manifests}),
        text=True,
        timeout=process.SHORT_COMMAND_TIMEOUT_SECONDS,
        check=True,
        stdout=process.DEVNULL,
    )
    _, report = expiry_report(record, settings.aws_credentials_warn_seconds, datetime.now(UTC))
    written = ", ".join(f"{namespace}/{CREDENTIALS_OBJECT}" for namespace in namespaces)
    print(f"{LOG} wrote {written}, {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
