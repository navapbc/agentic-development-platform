"""Evidence-artifact persistence for verified review attestations."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from database.models.schema import evidence_artifacts
from scripts.config import Settings, get_settings
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


@dataclass(frozen=True)
class EvidenceRecord:
    artifact_id: str
    execution_id: str
    content_hash: str
    source_component: str
    classification: str
    retention: str | None = None
    location: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class EvidenceSink(Protocol):
    async def persist(self, record: EvidenceRecord) -> str: ...


class EvidenceObjectStoreConfigError(RuntimeError):
    """The evidence object store is enabled but not usable."""


def resolve_retention(record: EvidenceRecord, settings: Settings) -> EvidenceRecord:
    """Return the record with its retention set, in days, from the record or the config.

    A record that carries its own numeric retention keeps it. Otherwise the configured
    ``evidence_retention_days`` is applied, so every stored record carries a retention.
    """
    current = (record.retention or "").strip()
    if current.isdigit():
        return record
    return replace(record, retention=str(settings.evidence_retention_days))


class PostgresEvidenceSink:
    def __init__(self, engine: AsyncEngine | None = None) -> None:
        self._engine = engine

    def _get_engine(self) -> AsyncEngine:
        if self._engine is None:
            self._engine = create_async_engine(
                get_settings().database_url, future=True, pool_pre_ping=True
            )
        return self._engine

    async def persist(self, record: EvidenceRecord) -> str:
        record = resolve_retention(record, get_settings())
        stmt = (
            insert(evidence_artifacts)
            .values(
                artifact_id=record.artifact_id,
                execution_id=record.execution_id,
                content_hash=record.content_hash,
                source_component=record.source_component,
                classification=record.classification,
                retention=record.retention,
                location=record.location,
                metadata=record.metadata or None,
            )
            .on_conflict_do_nothing(index_elements=[evidence_artifacts.c.content_hash])
        )
        engine = self._get_engine()
        async with engine.begin() as conn:
            await conn.execute(stmt)
        return record.artifact_id


class ObjectStoreWriter:
    """Writes content-addressed objects to an S3-compatible store under Object Lock.

    A repeated write of the same key is idempotent, and each written object carries an
    Object Lock retention that a delete or an overwrite cannot bypass within the window.
    Shared by the evidence sink and the audit path so both write the same way.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._client: Any = None

    @property
    def bucket(self) -> str:
        return self._settings.evidence_object_store_bucket

    def _get_client(self) -> Any:
        if self._client is None:
            import boto3

            s = self._settings
            kwargs: dict[str, Any] = {
                "region_name": s.evidence_object_store_region or s.aws_region
            }
            if s.evidence_object_store_endpoint_url:
                kwargs["endpoint_url"] = s.evidence_object_store_endpoint_url
            if s.evidence_object_store_access_key and s.evidence_object_store_secret_key:
                kwargs["aws_access_key_id"] = s.evidence_object_store_access_key
                kwargs["aws_secret_access_key"] = s.evidence_object_store_secret_key
            self._client = boto3.client("s3", **kwargs)
        return self._client

    def put_locked(self, key: str, body: bytes, *, retention_days: int) -> str:
        """Write ``body`` at ``key`` under Object Lock, skipping a re-put of the same key."""
        from botocore.exceptions import ClientError

        client = self._get_client()
        bucket = self.bucket
        location = f"s3://{bucket}/{key}"
        try:
            client.head_object(Bucket=bucket, Key=key)
            return location
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") not in ("404", "NoSuchKey", "NotFound"):
                raise
        retain_until = datetime.now(UTC) + timedelta(days=retention_days)
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            ContentType="application/json",
            ObjectLockMode=self._settings.evidence_object_lock_mode,
            ObjectLockRetainUntilDate=retain_until,
        )
        return location

    def check_reachable_and_locked(self) -> None:
        """Raise EvidenceObjectStoreConfigError when the bucket is unreachable or unlocked."""
        from botocore.exceptions import BotoCoreError, ClientError

        client = self._get_client()
        try:
            client.head_bucket(Bucket=self.bucket)
        except (ClientError, BotoCoreError) as exc:
            raise EvidenceObjectStoreConfigError(
                f"evidence bucket {self.bucket!r} is not reachable"
            ) from exc
        try:
            client.get_object_lock_configuration(Bucket=self.bucket)
        except (ClientError, BotoCoreError) as exc:
            raise EvidenceObjectStoreConfigError(
                f"evidence bucket {self.bucket!r} does not have Object Lock enabled"
            ) from exc


class ObjectStoreEvidenceSink:
    """Persist each evidence object to an S3-compatible store under Object Lock.

    The object body is content-addressed by the record's content hash, so a repeated
    write of the same content is idempotent. The object location is then recorded on the
    Postgres index so a reader resolves the object by id.
    """

    def __init__(
        self,
        index: EvidenceSink | None = None,
        settings: Settings | None = None,
        writer: ObjectStoreWriter | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._index = index or PostgresEvidenceSink()
        self._writer = writer or ObjectStoreWriter(self._settings)

    def _retention_days(self, record: EvidenceRecord) -> int:
        configured = (record.retention or "").strip()
        if configured.isdigit():
            return int(configured)
        return self._settings.evidence_retention_days

    async def persist(self, record: EvidenceRecord) -> str:
        record = resolve_retention(record, self._settings)
        key = f"evidence/{record.content_hash}"
        body = json.dumps(_serialize(record), sort_keys=True).encode("utf-8")
        location = await asyncio.to_thread(
            self._writer.put_locked, key, body, retention_days=self._retention_days(record)
        )
        return await self._index.persist(replace(record, location=location))


def _serialize(record: EvidenceRecord) -> dict[str, Any]:
    return {
        "artifact_id": record.artifact_id,
        "execution_id": record.execution_id,
        "content_hash": record.content_hash,
        "source_component": record.source_component,
        "classification": record.classification,
        "retention": record.retention,
        "metadata": record.metadata or {},
    }


async def persist_audit_object(
    event: dict[str, Any],
    settings: Settings | None = None,
    writer: ObjectStoreWriter | None = None,
) -> str | None:
    """Write an audit event to the object store under Object Lock, keyed by its event id.

    A no-op when the object store is disabled. The object is content-addressed by the
    event id, which is unique, so the Postgres row remains the queryable index and a
    reader resolves the object at ``audit/<event_id>`` without an extra column.
    """
    resolved = settings or get_settings()
    if not resolved.evidence_object_store_enabled:
        return None
    event_id = str(event["event_id"])
    key = f"audit/{event_id}"
    body = json.dumps(event, sort_keys=True, default=str).encode("utf-8")
    store = writer or ObjectStoreWriter(resolved)
    return await asyncio.to_thread(
        store.put_locked, key, body, retention_days=resolved.evidence_retention_days
    )


def build_evidence_sink(settings: Settings | None = None) -> EvidenceSink:
    """Return the object-store sink over the Postgres index when enabled, else Postgres."""
    resolved = settings or get_settings()
    if resolved.evidence_object_store_enabled:
        return ObjectStoreEvidenceSink(settings=resolved)
    return PostgresEvidenceSink()


def evidence_object_store_preflight(settings: Settings | None = None) -> None:
    """Fail the worker fast when the evidence object store is enabled but unusable.

    A no-op when the object store is disabled. When enabled it checks that boto3 is
    installed, the bucket is reachable, and Object Lock is enabled on the bucket, so a
    misconfiguration surfaces at startup rather than on the first evidence write.
    """
    resolved = settings or get_settings()
    if not resolved.evidence_object_store_enabled:
        return
    if not resolved.evidence_object_store_bucket:
        raise EvidenceObjectStoreConfigError(
            "EVIDENCE_OBJECT_STORE_BUCKET must be set when the evidence object store is enabled"
        )
    try:
        import boto3  # noqa: F401
    except ImportError as exc:
        raise EvidenceObjectStoreConfigError(
            "boto3 is required for the evidence object store; install the storage extra"
        ) from exc

    ObjectStoreWriter(resolved).check_reachable_and_locked()

