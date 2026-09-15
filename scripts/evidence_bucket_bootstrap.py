"""Create the evidence object-lock bucket for local development against MinIO.

The evidence object store is off by default. When a developer opts in by setting
EVIDENCE_OBJECT_STORE_ENABLED, the target bucket must exist with Object Lock enabled,
which requires versioning. This creates that bucket and sets a default retention so the
local path matches the deployed Object Lock behavior.
"""

from __future__ import annotations

from scripts.config import get_settings


def main() -> int:
    settings = get_settings()
    bucket = settings.evidence_object_store_bucket
    if not bucket:
        print("[evidence-bucket] set EVIDENCE_OBJECT_STORE_BUCKET first")
        return 1

    from scripts.aws_clients import configure_aws_fips

    configure_aws_fips(settings)

    import boto3
    from botocore.exceptions import ClientError

    kwargs: dict[str, object] = {
        "region_name": settings.evidence_object_store_region or settings.aws_region
    }
    if settings.evidence_object_store_endpoint_url:
        kwargs["endpoint_url"] = settings.evidence_object_store_endpoint_url
    if settings.evidence_object_store_access_key and settings.evidence_object_store_secret_key:
        kwargs["aws_access_key_id"] = settings.evidence_object_store_access_key
        kwargs["aws_secret_access_key"] = settings.evidence_object_store_secret_key
    client = boto3.client("s3", **kwargs)

    try:
        client.create_bucket(Bucket=bucket, ObjectLockEnabledForBucket=True)
        print(f"[evidence-bucket] created {bucket} with object lock")
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
            print(f"[evidence-bucket] {bucket} already exists")
        else:
            raise

    client.put_object_lock_configuration(
        Bucket=bucket,
        ObjectLockConfiguration={
            "ObjectLockEnabled": "Enabled",
            "Rule": {
                "DefaultRetention": {
                    "Mode": settings.evidence_object_lock_mode,
                    "Days": settings.evidence_object_lock_days,
                }
            },
        },
    )
    print(
        f"[evidence-bucket] default retention {settings.evidence_object_lock_days} days "
        f"in {settings.evidence_object_lock_mode} mode"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
