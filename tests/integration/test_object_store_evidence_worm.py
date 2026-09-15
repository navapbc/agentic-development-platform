"""Live WORM test for the object-store evidence sink against MinIO.

Exercises the write-once-read-many guarantee that AWS S3 Object Lock enforces in a
deployed environment. It creates an Object-Lock bucket, writes an evidence object under
a compliance retention through the sink, and asserts that the locked version cannot be
deleted or overwritten within the window and that a read returns the original bytes.

Skipped unless MinIO is reachable at the configured endpoint.
"""

from __future__ import annotations

import socket
import uuid
from urllib.parse import urlparse

import pytest
from botocore.exceptions import ClientError
from services.review.evidence import EvidenceRecord, ObjectStoreEvidenceSink
from tests.support import hermetic_settings

_ENDPOINT = "http://localhost:9090"
_ACCESS_KEY = "minio"
_SECRET_KEY = "change-me-locally"


def _minio_reachable() -> bool:
    parsed = urlparse(_ENDPOINT)
    try:
        with socket.create_connection((parsed.hostname, parsed.port or 80), timeout=1):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(not _minio_reachable(), reason="MinIO must be reachable")


class _CapturingIndex:
    def __init__(self) -> None:
        self.records: list[EvidenceRecord] = []

    async def persist(self, record: EvidenceRecord) -> str:
        self.records.append(record)
        return record.artifact_id


def _settings(bucket: str):
    return hermetic_settings(
        evidence_object_store_enabled=True,
        evidence_object_store_endpoint_url=_ENDPOINT,
        evidence_object_store_bucket=bucket,
        evidence_object_store_region="us-east-1",
        evidence_object_store_access_key=_ACCESS_KEY,
        evidence_object_store_secret_key=_SECRET_KEY,
        evidence_object_lock_mode="COMPLIANCE",
        evidence_retention_days=1,
    )


async def test_locked_evidence_object_resists_delete_and_overwrite():
    bucket = f"evidence-worm-{uuid.uuid4().hex[:12]}"
    settings = _settings(bucket)
    index = _CapturingIndex()
    sink = ObjectStoreEvidenceSink(index=index, settings=settings)
    client = sink._writer._get_client()
    client.create_bucket(Bucket=bucket, ObjectLockEnabledForBucket=True)

    from services.review.evidence import evidence_object_store_preflight

    evidence_object_store_preflight(settings)

    record = EvidenceRecord(
        artifact_id="art-1",
        execution_id="exec-1",
        content_hash=f"sha256:{uuid.uuid4().hex}",
        source_component="gate:code-review",
        classification="final",
    )
    await sink.persist(record)

    key = f"evidence/{record.content_hash}"
    head = client.head_object(Bucket=bucket, Key=key)
    version_id = head["VersionId"]
    assert head["ObjectLockMode"] == "COMPLIANCE"
    original = client.get_object(Bucket=bucket, Key=key)["Body"].read()

    with pytest.raises(ClientError):
        client.delete_object(Bucket=bucket, Key=key, VersionId=version_id)

    client.put_object(Bucket=bucket, Key=key, Body=b"tampered")
    locked = client.get_object(Bucket=bucket, Key=key, VersionId=version_id)["Body"].read()
    assert locked == original

    assert index.records[-1].location == f"s3://{bucket}/{key}"

    from services.review.evidence import persist_audit_object

    audit_location = await persist_audit_object(
        {"event_id": f"e-{uuid.uuid4().hex}", "event_type": "REVIEW_COMPLETED"},
        settings=settings,
    )
    assert audit_location is not None
    audit_key = audit_location.removeprefix(f"s3://{bucket}/")
    assert audit_key.startswith("audit/")
    assert client.head_object(Bucket=bucket, Key=audit_key)["ObjectLockMode"] == "COMPLIANCE"
