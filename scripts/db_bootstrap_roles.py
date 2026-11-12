"""Create the per-service database roles and databases through the secret-store function.

The secret-store module's PostgreSQL function runs the bootstrap through the RDS Data API
as the RDS-managed master user, so this needs AWS credentials that allow
lambda:InvokeFunction and no network path to the cluster. Run it once after the first
apply and again after a database is restored. Each run is idempotent and resets every
role password to the current value of its secret.
"""

from __future__ import annotations

import argparse
import json

from scripts.config import get_settings

FUNCTION_TIMEOUT_SECONDS = 900
INVOKE_READ_TIMEOUT_SECONDS = FUNCTION_TIMEOUT_SECONDS + 60


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--function-name",
        required=True,
        help="The role_bootstrap_function_name output of the composition root.",
    )
    parser.add_argument("--region", default="", help="Function region. Defaults to AWS_REGION.")
    args = parser.parse_args(argv)
    settings = get_settings()

    from scripts.aws_clients import configure_aws_fips

    configure_aws_fips(settings)

    import boto3
    from botocore.config import Config

    # The bootstrap waits for a paused cluster to resume, which outlasts botocore's 60 second
    # read timeout, and a retried invoke races the first one on CREATE ROLE.
    client = boto3.client(
        "lambda",
        region_name=args.region or settings.aws_region,
        config=Config(
            read_timeout=INVOKE_READ_TIMEOUT_SECONDS,
            connect_timeout=10,
            retries={"max_attempts": 0},
        ),
    )
    response = client.invoke(
        FunctionName=args.function_name,
        Payload=json.dumps({"action": "bootstrap"}).encode("utf-8"),
    )
    payload = json.loads(response["Payload"].read() or b"null")
    if response.get("FunctionError"):
        message = payload.get("errorMessage", payload) if isinstance(payload, dict) else payload
        print(f"[db-bootstrap-roles] failed: {message}")
        return 1
    print(f"[db-bootstrap-roles] configured roles {', '.join(payload['roles'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
