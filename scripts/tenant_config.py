"""Validate a tenant config bundle, or validate it and publish it to the tenant config bucket.

The bundle is a directory of tenant config files, by default config/local/tenants/<tenant>/.
Validation reads a replaceable file the bundle lacks from the platform directory, which
--platform-dir points at when the command runs outside a platform checkout, such as in a tenant's
CI. The workers validate every bundle again against the deployment's platform config before they
activate it.

On a workstation, config/local/tenants/<tenant>.env holds the settings and credentials
only that tenant's pods receive. Validation applies its settings over config/local/platform.env,
so a tenant whose file sets TICKET_BACKEND is validated against its own ticket backend.

Validation also refuses a bundle that claims a value another tenant holds, such as the channel of a
tenant on the shared Slack app, a Slack app ID, or a Jira project and intake label. The other
tenants are each tenant directory under the platform directory's tenants/.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.config import Settings

LOG = "[tenant-config]"


def read_tenant_settings(tenants_dir: Path, tenant: str) -> dict[str, str] | None:
    """Return the values of the tenant's settings file in tenants_dir, or None without one."""
    path = tenants_dir / f"{tenant}.env"
    if not path.is_file():
        return None
    from dotenv import dotenv_values

    return {key: value for key, value in dotenv_values(path).items() if value is not None}


def apply_tenant_settings(settings: Settings, values: Mapping[str, str]) -> Settings:
    """Return a copy of settings with the tenant file's values applied, each validated.

    A key that names no setting is ignored, as in config/local/platform.env.
    """
    merged = settings.model_copy()
    fields = type(settings).model_fields
    for key, value in values.items():
        if key.lower() in fields:
            type(settings).__pydantic_validator__.validate_assignment(merged, key.lower(), value)
    return merged


def _settings(tenant: str):
    from services.integration.credentials import env_config_dir

    from scripts.config import get_settings

    settings = get_settings()
    name = tenant or settings.tenant
    if name:
        own = read_tenant_settings(env_config_dir() / "tenants", name)
        if own:
            settings = apply_tenant_settings(settings, own)
    return settings.model_copy(update={"tenant": name})


def bundle_files(directory: Path) -> dict[str, str]:
    from services.integration.tenant_config import bundle_file_name

    if not directory.is_dir():
        raise SystemExit(f"{LOG} {directory} is not a directory")
    return {
        name: entry.read_text(encoding="utf-8")
        for entry in sorted(directory.iterdir())
        if entry.is_file() and (name := bundle_file_name(entry.name)) is not None
    }


def tenant_files(directory: Path) -> dict[str, str]:
    return bundle_files(directory) if directory.is_dir() else {}


def _other_tenant_claims(platform_dir: Path, tenant: str) -> dict[str, frozenset]:
    from services.integration.tenant_claims import bundle_claims
    from services.orchestration.task_queues import TENANT_NAME

    from scripts.config import get_settings

    base = get_settings()
    tenants_dir = platform_dir / "tenants"
    names = {
        entry.name
        for entry in (tenants_dir.iterdir() if tenants_dir.is_dir() else ())
        if entry.is_dir() and TENANT_NAME.fullmatch(entry.name)
    }
    names.discard(tenant)
    claims: dict[str, frozenset] = {}
    for name in sorted(names):
        backend = _ticket_backend(base, tenants_dir, name)
        claims[name] = bundle_claims(tenant_files(tenants_dir / name), backend)
    return claims


def _ticket_backend(settings: Settings, tenants_dir: Path, tenant: str) -> str:
    own = read_tenant_settings(tenants_dir, tenant) or {}
    return apply_tenant_settings(settings, own).ticket_backend


def _claims_clash(claims: frozenset, directory: Path, settings) -> bool:
    """Report each value the bundle claims that another tenant claims."""
    from services.integration.credentials import ConfigError, env_config_dir
    from services.integration.tenant_claims import find_clashes

    try:
        others = _other_tenant_claims(env_config_dir(), settings.tenant)
    except ConfigError as exc:
        print(f"{LOG} {settings.tenant}: {exc}", file=sys.stderr)
        return True
    clashes = [
        clash
        for clash in find_clashes({**others, settings.tenant: claims})
        if settings.tenant in clash.tenants
    ]
    for clash in clashes:
        print(
            f"{LOG} {settings.tenant}: {clash}. Give {settings.tenant} a value of its own in "
            f"{directory}",
            file=sys.stderr,
        )
    return bool(clashes)


def _s3_client(settings):
    from services.integration.tenant_config import config_bucket_client

    return config_bucket_client(settings)


def _probe_client(settings, bucket: str, client):
    """Return the client the claims probe lists with, or None without a bucket or credential."""
    if not bucket:
        return None
    if client is not None:
        return client
    import boto3
    from botocore.exceptions import ProfileNotFound

    try:
        if boto3.Session().get_credentials() is None:
            return None
    except ProfileNotFound:
        return None
    return _s3_client(settings)


def _claims_probe(claims: frozenset, settings, bucket: str, client) -> bool:
    """Report each claim of the bundle whose earliest marker belongs to another tenant, and refuse
    a bundle on the shared Slack app while the bucket lacks the claims-protocol object."""
    from services.integration.tenant_claims import (
        CLAIMS_PROTOCOL_ABSENT,
        ClaimStore,
        claims_protocol_in_force,
        names_shared_app,
    )

    from scripts.aws_clients import error_code

    probe = _probe_client(settings, bucket, client)
    if probe is None:
        print(
            f"{LOG} {settings.tenant}: skipped the claims probe and the claims-protocol check, "
            "since no config bucket or AWS credential is set"
        )
        return True
    from botocore.exceptions import BotoCoreError, ClientError

    store = ClaimStore(probe, bucket)
    try:
        if names_shared_app(claims) and not claims_protocol_in_force(probe, bucket):
            print(f"{LOG} {settings.tenant}: {CLAIMS_PROTOCOL_ABSENT}", file=sys.stderr)
            return False
        contested = [
            check
            for claim in sorted(claims)
            if (check := store.check(settings.tenant, claim)).contested
        ]
    except ClientError as exc:
        print(
            f"{LOG} {settings.tenant}: the claims probe failed with {error_code(exc)}",
            file=sys.stderr,
        )
        return False
    except BotoCoreError as exc:
        print(
            f"{LOG} {settings.tenant}: the claims probe failed with {type(exc).__name__}",
            file=sys.stderr,
        )
        return False
    for check in contested:
        print(f"{LOG} {settings.tenant}: {check}", file=sys.stderr)
    return not contested


def validate(directory: Path, settings, bucket: str = "", client=None) -> bool:
    """Validate the bundle, refuse a value another local tenant claims, and probe the bucket."""
    return _validated_claims(directory, settings, bucket, client) is not None


def _validated_claims(directory: Path, settings, bucket: str, client) -> frozenset | None:
    """Return the claims of a valid bundle, or None after reporting why the bundle is invalid."""
    from services.integration.credentials import ConfigError
    from services.integration.tenant_claims import bundle_claims
    from services.integration.tenant_config import validate_bundle

    if not settings.tenant:
        raise SystemExit(f"{LOG} name the tenant with --tenant or TENANT")
    with tempfile.TemporaryDirectory(prefix="tenant-config-") as staging:
        entries = sorted(directory.iterdir()) if directory.is_dir() else []
        for entry in entries:
            if entry.is_file():
                shutil.copy2(entry, Path(staging) / entry.name)
        try:
            validate_bundle(Path(staging), settings)
        except ConfigError as exc:
            print(f"{LOG} {settings.tenant}: {exc}", file=sys.stderr)
            return None
    try:
        claims = bundle_claims(tenant_files(directory), settings.ticket_backend)
    except ConfigError as exc:
        print(f"{LOG} {settings.tenant}: {exc}", file=sys.stderr)
        return None
    if _claims_clash(claims, directory, settings):
        return None
    if not _claims_probe(claims, settings, bucket or settings.tenant_config_bucket, client):
        return None
    print(f"{LOG} {settings.tenant}: the bundle in {directory} is valid")
    return claims


def publish(
    directory: Path,
    settings,
    bucket: str,
    client=None,
    claims: frozenset | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, str]:
    """Claim the bundle's values, upload every file, and release the claims it no longer names.

    The index first gains the new claims, so a publish that fails at any later step leaves extra
    claims and never fewer. A claim another tenant holds refuses the publish before any upload,
    and a bundle on the shared Slack app is refused before any write while the bucket lacks the
    claims-protocol object.

    A shared app channel the bundle no longer names is retained, not released, until the button
    lifetime of the published escalation.yaml it replaces plus two refresh intervals has passed,
    so another tenant cannot claim it while buttons posted in it can still be clicked. A later
    publish releases each retained channel past that time.
    """
    from services.integration.credentials import ConfigError
    from services.integration.tenant_claims import (
        CLAIMS_PROTOCOL_ABSENT,
        CLAIMS_PROTOCOL_TAG,
        ClaimStore,
        bundle_claims,
        claims_protocol_in_force,
        names_shared_app,
    )
    from services.integration.tenant_config import S3Source

    if client is None:
        client = _s3_client(settings)
    published_at = now or datetime.now(UTC)
    tenant = settings.tenant
    files = bundle_files(directory)
    if claims is None:
        claims = bundle_claims(files, settings.ticket_backend)
    if names_shared_app(claims) and not claims_protocol_in_force(client, bucket):
        raise ConfigError(CLAIMS_PROTOCOL_ABSENT)
    store = ClaimStore(client, bucket)
    source = S3Source(client, bucket, tenant)
    held = store.extend_index(tenant, claims)
    window = timedelta(
        hours=_published_token_ttl_hours(source) if held.leaving(claims) else 0,
        seconds=2 * settings.tenant_config_refresh_seconds,
    )
    for check in store.claim(tenant, claims):
        for rival in check.rivals:
            print(f"{LOG} {tenant}: tenant {rival} also claims {check.claim}", file=sys.stderr)
    for name, text in files.items():
        client.put_object(
            Bucket=bucket,
            Key=f"{source.prefix}{name}.yaml",
            Body=text.encode(),
            Tagging=CLAIMS_PROTOCOL_TAG,
        )
    for name in sorted(set(source.latest().files) - set(files)):
        client.delete_object(Bucket=bucket, Key=f"{source.prefix}{name}.yaml")
    store.reconcile(tenant, claims, held, published_at=published_at, window=window)
    return dict(source.latest().files)


def _published_token_ttl_hours(source) -> int:
    """Return the button lifetime of the tenant's published escalation.yaml, which signed the
    buttons already posted."""
    import yaml
    from services.integration.credentials import ConfigError
    from services.integration.tenant_claims import effective_config
    from services.orchestration.escalation import (
        DEFAULT_TOKEN_TTL_HOURS,
        ESCALATION_CONFIG,
        slack_token_ttl_hours,
    )

    files = source.fetch_files(source.latest(), [ESCALATION_CONFIG])
    try:
        raw = effective_config(files, ESCALATION_CONFIG)
        slack = raw.get("slack") if isinstance(raw, Mapping) else None
        return (
            slack_token_ttl_hours(slack) if isinstance(slack, Mapping) else DEFAULT_TOKEN_TTL_HOURS
        )
    except ConfigError, yaml.YAMLError:
        return DEFAULT_TOKEN_TTL_HOURS


def _published_claims(settings, bucket: str, client) -> dict[str, frozenset]:
    """Return the claims of each tenant's published bundle, read with admin credentials.

    Each tenant's ticket backend is the deployment's, with that tenant's own settings file applied,
    never the settings file of the tenant running the command.
    """
    from services.integration.credentials import env_config_dir
    from services.integration.tenant_claims import CLAIM_FILES, bundle_claims
    from services.integration.tenant_config import S3Source

    from scripts.config import get_settings

    base = get_settings()
    tenants_dir = env_config_dir() / "tenants"
    paginator = client.get_paginator("list_objects_v2")
    tenants = sorted(
        folder["Prefix"].split("/")[1]
        for page in paginator.paginate(Bucket=bucket, Prefix="tenants/", Delimiter="/")
        for folder in page.get("CommonPrefixes") or []
    )
    claims: dict[str, frozenset] = {}
    for tenant in tenants:
        source = S3Source(client, bucket, tenant)
        identity = source.latest()
        if not identity.files:
            continue
        files = source.fetch_files(identity, CLAIM_FILES)
        backend = _ticket_backend(base, tenants_dir, tenant)
        claims[tenant] = bundle_claims(files, backend)
    return claims


def _marker_findings(
    store, published: Mapping[str, frozenset], now: datetime
) -> tuple[list[str], list[str]]:
    """Describe each marker whose tenant's published bundle does not name its value, and each
    channel a tenant retains inside its window, which is no finding."""
    indexes = store.indexes()
    named = {(claim.kind, claim.digest): claim for claims in published.values() for claim in claims}
    for index in indexes.values():
        for claim in (*index.claims, *index.retained):
            named.setdefault((claim.kind, claim.digest), claim)
    published_keys = {
        tenant: {(claim.kind, claim.digest) for claim in claims}
        for tenant, claims in published.items()
    }
    retained_until = {
        (tenant, claim.kind, claim.digest): (claim, at)
        for tenant, index in indexes.items()
        for claim, at in index.retained.items()
    }
    findings, retained = [], []
    for kind, digest, tenant in store.markers():
        if (kind, digest) in published_keys.get(tenant, ()):
            continue
        if (tenant, kind, digest) in retained_until:
            claim, at = retained_until[(tenant, kind, digest)]
            if at > now:
                retained.append(f"tenant {tenant} retains {claim} until {at.isoformat()}")
            else:
                findings.append(
                    f"tenant {tenant} retains {claim} past its release time {at.isoformat()}, "
                    "and claims-sweep releases it now"
                )
            continue
        claim = named.get((kind, digest))
        value = str(claim) if claim else f"{kind} {digest}"
        finding = (
            f"tenant {tenant} holds a marker on {value} that no published bundle of {tenant} names"
        )
        owners = sorted(
            other for other, claims in published.items() if other != tenant and claim in claims
        )
        if owners:
            finding += (
                f", a squat on a value the published bundle of tenant {' and '.join(owners)} names"
            )
        findings.append(finding)
    return findings, retained


def _print_findings(lines: list[str], retained: list[str] = ()) -> int:
    for line in retained:
        print(f"{LOG} {line}")
    for line in lines:
        print(f"{LOG} {line}", file=sys.stderr)
    return 1 if lines else 0


def report(settings, bucket: str, client=None, *, now: datetime | None = None) -> int:
    """Report every clash between published bundles and every marker no published bundle names.

    A channel a tenant retains after leaving it is listed with its release time, and is a finding
    only once that time has passed.
    """
    from services.integration.tenant_claims import ClaimStore, find_clashes

    client = client or _s3_client(settings)
    store = ClaimStore(client, bucket)
    published = _published_claims(settings, bucket, client)
    lines = [_contested(store, clash) for clash in find_clashes(published)]
    findings, retained = _marker_findings(store, published, now or datetime.now(UTC))
    return _print_findings(lines + findings, retained)


def _contested(store, clash) -> str:
    """Describe a clash with the tenant whose marker holds the value, which an admin keeps or
    releases once the value's owner is confirmed."""
    markers = [
        marker
        for kind_markers in store.contending_markers(clash.claims[0]).values()
        for marker in kind_markers
        if marker.tenant in clash.tenants
    ]
    if not markers:
        return f"{clash}, and neither holds a marker on it"
    earliest = min(marker.last_modified for marker in markers)
    holders = sorted({marker.tenant for marker in markers if marker.last_modified == earliest})
    if len(holders) > 1:
        return f"{clash}, and their markers have one time, so neither holds it"
    return (
        f"{clash}, and tenant {holders[0]} holds the earliest marker. Confirm which tenant owns "
        f"the value, and release the other tenant's marker with claims-release"
    )


def sweep(settings, bucket: str, client=None, *, now: datetime | None = None) -> int:
    """Release every retained channel past its release time, whichever tenant retains it, as an
    admin, so a tenant that never publishes again does not hold a channel it left."""
    from services.integration.tenant_claims import ClaimStore

    client = client or _s3_client(settings)
    store = ClaimStore(client, bucket)
    swept_at = now or datetime.now(UTC)
    for tenant, index in sorted(store.indexes().items()):
        for claim, release_after in sorted(index.retained.items()):
            if release_after <= swept_at:
                store.drop(tenant, claim)
                print(
                    f"{LOG} released {claim}, which tenant {tenant} retained until "
                    f"{release_after.isoformat()}"
                )
    return 0


def backfill(settings, bucket: str, client=None) -> int:
    """Claim every value of the published bundles that only one tenant names, then report.

    A value two published bundles name stays unclaimed until an admin decides which tenant keeps
    it, so the backfill reports the pair and exits non-zero.
    """
    from services.integration.tenant_claims import ClaimStore, find_clashes

    client = client or _s3_client(settings)
    store = ClaimStore(client, bucket)
    published = _published_claims(settings, bucket, client)
    clashes = find_clashes(published)
    contested = {claim for clash in clashes for claim in clash.claims}
    for tenant, claims in published.items():
        unclashed = claims - contested
        store.extend_index(tenant, unclashed)
        for claim in sorted(unclashed):
            store.create(tenant, claim)
    findings, retained = _marker_findings(store, published, datetime.now(UTC))
    return _print_findings([str(clash) for clash in clashes] + findings, retained)


def release(settings, bucket: str, kind: str, value: str, client=None) -> None:
    """Remove the tenant's marker on one value and the value from the tenant's index, whether
    the index holds it as a current claim or as a retained channel."""
    from services.integration.tenant_claims import ClaimStore, canonical_claim

    client = client or _s3_client(settings)
    ClaimStore(client, bucket).drop(settings.tenant, canonical_claim(kind, value))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "validate",
            "publish",
            "claims-backfill",
            "claims-report",
            "claims-release",
            "claims-sweep",
        ],
    )
    parser.add_argument("--tenant", default="", help="Tenant name. Defaults to TENANT.")
    parser.add_argument("--dir", default="", help="Bundle directory.")
    parser.add_argument("--platform-dir", default="", help="Platform config directory.")
    parser.add_argument(
        "--bucket", default="", help="Config bucket. Defaults to TENANT_CONFIG_BUCKET."
    )
    parser.add_argument("--kind", default="", help="Claim kind, for claims-release.")
    parser.add_argument("--value", default="", help="Claim value, for claims-release.")
    args = parser.parse_args(argv)

    if args.platform_dir:
        os.environ["PLATFORM_ENV_CONFIG_DIR"] = str(Path(args.platform_dir).resolve())
    settings = _settings(args.tenant)
    from services.integration.credentials import ConfigError, env_config_dir
    from services.integration.tenant_config import refuse_platform_tenant_files

    if args.command.startswith("claims-"):
        bucket = args.bucket or settings.tenant_config_bucket
        if not bucket:
            raise SystemExit(f"{LOG} name the config bucket with --bucket or TENANT_CONFIG_BUCKET")
        if args.command == "claims-report":
            return report(settings, bucket)
        if args.command == "claims-backfill":
            return backfill(settings, bucket)
        if args.command == "claims-sweep":
            return sweep(settings, bucket)
        if not (settings.tenant and args.kind and args.value):
            raise SystemExit(f"{LOG} claims-release needs --tenant, --kind, and --value")
        release(settings, bucket, args.kind, args.value)
        print(f"{LOG} released {args.kind} {args.value} from tenant {settings.tenant}")
        return 0

    try:
        refuse_platform_tenant_files(env_config_dir())
    except ConfigError as exc:
        print(f"{LOG} {exc}", file=sys.stderr)
        return 1
    directory = Path(args.dir) if args.dir else env_config_dir() / "tenants" / settings.tenant
    bucket = args.bucket or settings.tenant_config_bucket
    client = _s3_client(settings) if args.command == "publish" and bucket else None
    claims = _validated_claims(directory, settings, bucket, client)
    if claims is None:
        return 1
    if args.command == "validate":
        return 0
    if not bucket:
        raise SystemExit(f"{LOG} name the config bucket with --bucket or TENANT_CONFIG_BUCKET")
    try:
        versions = publish(directory, settings, bucket, client, claims)
    except ConfigError as exc:
        print(f"{LOG} {settings.tenant}: {exc}", file=sys.stderr)
        return 1
    print(f"{LOG} published {len(versions)} file(s) to s3://{bucket}/tenants/{settings.tenant}/")
    for name, version in sorted(versions.items()):
        print(f"{LOG}   {name}.yaml version {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
