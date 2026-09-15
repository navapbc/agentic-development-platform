"""Unit tests for the object-store evidence sink with a fake S3 client.

These exercise the content-addressed write, the Object Lock retention arguments, the
idempotent skip on a repeated write, and the object location recorded on the index,
without a running object store.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import pytest
from botocore.exceptions import ClientError
from services.review.evidence import (
    EvidenceRecord,
    ObjectStoreEvidenceSink,
    ObjectStoreWriter,
    persist_audit_object,
)
from tests.support import hermetic_settings


class _FakeS3:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.puts: list[dict] = []

    def head_object(self, *, Bucket: str, Key: str) -> dict:
        if (Bucket, Key) not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {}

    def put_object(self, **kwargs: object) -> dict:
        self.puts.append(kwargs)
        self.objects[(str(kwargs["Bucket"]), str(kwargs["Key"]))] = bytes(kwargs["Body"])  # type: ignore[arg-type]
        return {}


class _CapturingIndex:
    def __init__(self) -> None:
        self.records: list[EvidenceRecord] = []

    async def persist(self, record: EvidenceRecord) -> str:
        self.records.append(record)
        return record.artifact_id


def _record(**overrides) -> EvidenceRecord:
    record = EvidenceRecord(
        artifact_id="art-1",
        execution_id="exec-1",
        content_hash="sha256:deadbeef",
        source_component="gate:code-review",
        classification="final",
    )
    return replace(record, **overrides) if overrides else record


def _settings(**overrides):
    return hermetic_settings(
        evidence_object_store_enabled=True,
        evidence_object_store_bucket="evidence",
        evidence_object_lock_mode="COMPLIANCE",
        evidence_retention_days=30,
        **overrides,
    )


def _writer(client: _FakeS3) -> ObjectStoreWriter:
    writer = ObjectStoreWriter(_settings())
    writer._client = client
    return writer


def _sink(index: _CapturingIndex, client: _FakeS3) -> ObjectStoreEvidenceSink:
    return ObjectStoreEvidenceSink(index=index, settings=_settings(), writer=_writer(client))


async def test_write_puts_content_addressed_object_under_object_lock():
    index, client = _CapturingIndex(), _FakeS3()
    await _sink(index, client).persist(_record())

    assert len(client.puts) == 1
    put = client.puts[0]
    assert put["Bucket"] == "evidence"
    assert put["Key"] == "evidence/sha256:deadbeef"
    assert put["ObjectLockMode"] == "COMPLIANCE"
    assert isinstance(put["ObjectLockRetainUntilDate"], datetime)


async def test_index_record_carries_the_object_location():
    index, client = _CapturingIndex(), _FakeS3()
    await _sink(index, client).persist(_record())

    assert index.records[-1].location == "s3://evidence/evidence/sha256:deadbeef"


async def test_repeated_write_of_same_content_is_idempotent():
    index, client = _CapturingIndex(), _FakeS3()
    sink = _sink(index, client)
    await sink.persist(_record())
    await sink.persist(_record())

    assert len(client.puts) == 1
    assert len(index.records) == 2


async def test_record_retention_days_override_the_configured_window():
    index, client = _CapturingIndex(), _FakeS3()
    before = datetime.now().astimezone()
    await _sink(index, client).persist(_record(retention="1"))

    retain_until = client.puts[0]["ObjectLockRetainUntilDate"]
    assert (retain_until - before).days < 2


async def test_configured_retention_is_applied_to_a_record_without_one():
    index, client = _CapturingIndex(), _FakeS3()
    await _sink(index, client).persist(_record())

    assert index.records[-1].retention == "30"


async def test_a_records_own_retention_is_respected():
    index, client = _CapturingIndex(), _FakeS3()
    await _sink(index, client).persist(_record(retention="5"))

    assert index.records[-1].retention == "5"


def test_resolve_retention_fills_unset_and_keeps_numeric():
    from services.review.evidence import resolve_retention

    settings = _settings()
    assert resolve_retention(_record(), settings).retention == "30"
    assert resolve_retention(_record(retention="5"), settings).retention == "5"
    assert resolve_retention(_record(retention=""), settings).retention == "30"


async def test_head_object_non_404_error_propagates():
    index, client = _CapturingIndex(), _FakeS3()

    def _raise(*, Bucket: str, Key: str) -> dict:
        raise ClientError({"Error": {"Code": "AccessDenied"}}, "HeadObject")

    client.head_object = _raise  # type: ignore[method-assign]
    with pytest.raises(ClientError):
        await _sink(index, client).persist(_record())


def test_preflight_is_a_noop_when_disabled():
    from services.review.evidence import evidence_object_store_preflight

    evidence_object_store_preflight(hermetic_settings(evidence_object_store_enabled=False))


def test_preflight_raises_when_bucket_unset():
    from services.review.evidence import (
        EvidenceObjectStoreConfigError,
        evidence_object_store_preflight,
    )

    settings = hermetic_settings(
        evidence_object_store_enabled=True, evidence_object_store_bucket=""
    )
    with pytest.raises(EvidenceObjectStoreConfigError):
        evidence_object_store_preflight(settings)


async def test_audit_object_is_a_noop_when_disabled():
    client = _FakeS3()
    result = await persist_audit_object(
        {"event_id": "e1"},
        settings=hermetic_settings(evidence_object_store_enabled=False),
        writer=_writer(client),
    )
    assert result is None
    assert client.puts == []


async def test_audit_object_writes_under_the_audit_prefix():
    client = _FakeS3()
    location = await persist_audit_object(
        {"event_id": "e1", "event_type": "REVIEW_COMPLETED"},
        settings=_settings(),
        writer=_writer(client),
    )
    assert location == "s3://evidence/audit/e1"
    assert client.puts[0]["Key"] == "audit/e1"
    assert client.puts[0]["ObjectLockMode"] == "COMPLIANCE"


async def test_audit_object_repeated_write_is_idempotent():
    client = _FakeS3()
    writer = _writer(client)
    await persist_audit_object({"event_id": "e1"}, settings=_settings(), writer=writer)
    await persist_audit_object({"event_id": "e1"}, settings=_settings(), writer=writer)
    assert len(client.puts) == 1
