"""Validate a tenant config bundle, or validate it and publish it to the tenant config bucket.

The bundle is a directory of tenant config files, by default config/<environment>/tenants/<tenant>/.
Validation layers it over the environment directory, which --environment-dir points at when the
command runs outside a platform checkout, such as in a tenant's CI. The workers validate every
bundle again against the deployment's environment before they activate it.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

LOG = "[tenant-config]"


def _settings(tenant: str):
    from scripts.config import get_settings

    settings = get_settings()
    return settings.model_copy(update={"tenant": tenant or settings.tenant})


def bundle_files(directory: Path) -> dict[str, str]:
    from services.integration.tenant_config import bundle_file_name

    if not directory.is_dir():
        raise SystemExit(f"{LOG} {directory} is not a directory")
    return {
        name: entry.read_text(encoding="utf-8")
        for entry in sorted(directory.iterdir())
        if entry.is_file() and (name := bundle_file_name(entry.name)) is not None
    }


def validate(directory: Path, settings) -> bool:
    from services.integration.credentials import ConfigError
    from services.integration.tenant_config import validate_bundle

    if not settings.tenant:
        raise SystemExit(f"{LOG} name the tenant with --tenant or TENANT")
    with tempfile.TemporaryDirectory(prefix="tenant-config-") as staging:
        entries = sorted(directory.iterdir()) if directory.is_dir() else []
        if not entries:
            print(
                f"{LOG} {settings.tenant}: {directory} is empty, so the environment's files apply"
            )
        for entry in entries:
            if entry.is_file():
                shutil.copy2(entry, Path(staging) / entry.name)
        try:
            validate_bundle(Path(staging), settings)
        except ConfigError as exc:
            print(f"{LOG} {settings.tenant}: {exc}", file=sys.stderr)
            return False
    print(f"{LOG} {settings.tenant}: the bundle in {directory} is valid")
    return True


def publish(directory: Path, settings, bucket: str, client=None) -> dict[str, str]:
    """Upload every file of the bundle and delete the prefix's files the bundle no longer holds."""
    from services.integration.tenant_config import S3Source

    if client is None:
        from scripts.aws_clients import configure_aws_fips, s3_client

        configure_aws_fips(settings)
        client = s3_client(region=settings.aws_region)
    source = S3Source(client, bucket, settings.tenant)
    files = bundle_files(directory)
    for name, text in files.items():
        client.put_object(Bucket=bucket, Key=f"{source.prefix}{name}.yaml", Body=text.encode())
    for name in sorted(set(source.latest().files) - set(files)):
        client.delete_object(Bucket=bucket, Key=f"{source.prefix}{name}.yaml")
    return dict(source.latest().files)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["validate", "publish"])
    parser.add_argument("--tenant", default="", help="Tenant name. Defaults to TENANT.")
    parser.add_argument("--dir", default="", help="Bundle directory.")
    parser.add_argument("--environment-dir", default="", help="Environment config directory.")
    parser.add_argument(
        "--bucket", default="", help="Config bucket. Defaults to TENANT_CONFIG_BUCKET."
    )
    args = parser.parse_args(argv)

    if args.environment_dir:
        os.environ["PLATFORM_ENV_CONFIG_DIR"] = str(Path(args.environment_dir).resolve())
    settings = _settings(args.tenant)
    from services.integration.credentials import env_config_dir

    directory = (
        Path(args.dir)
        if args.dir
        else env_config_dir(settings.environment) / "tenants" / settings.tenant
    )
    if not validate(directory, settings):
        return 1
    if args.command == "validate":
        return 0
    bucket = args.bucket or settings.tenant_config_bucket
    if not bucket:
        raise SystemExit(f"{LOG} name the config bucket with --bucket or TENANT_CONFIG_BUCKET")
    versions = publish(directory, settings, bucket)
    print(f"{LOG} published {len(versions)} file(s) to s3://{bucket}/tenants/{settings.tenant}/")
    for name, version in sorted(versions.items()):
        print(f"{LOG}   {name}.yaml version {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
