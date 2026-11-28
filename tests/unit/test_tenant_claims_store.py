"""Claim markers in the tenant config bucket, where the earliest marker on a value holds it."""

from __future__ import annotations

import hashlib
import io
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError
from services.integration.tenant_claims import (
    CLAIMS_PROTOCOL_KEY,
    JIRA_PROJECT,
    SLACK_APP,
    SLACK_CHANNEL,
    SLACK_OWN_CHANNEL,
    Claim,
    ClaimStore,
    canonical_claim,
)


def _error(code: str, operation: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


class FakeS3:
    def __init__(self, missing_code: str = "NoSuchKey", *, claims_protocol: bool = True) -> None:
        self.objects: dict[str, list[dict]] = {}
        if claims_protocol:
            self.objects[CLAIMS_PROTOCOL_KEY] = [
                {"VersionId": "v1", "Body": b"{}", "Tagging": None, "LastModified": None}
            ]
        self.clock = datetime(2026, 11, 1, tzinfo=UTC)
        self.step = timedelta(seconds=1)
        self.missing_code = missing_code
        self.failing_prefix = ""
        self.writes: list[str] = []
        self.reads: list[str] = []

    def put_object(self, *, Bucket, Key, Body, IfNoneMatch=None, Tagging=None, ContentType=None):
        if self.failing_prefix and Key.startswith(self.failing_prefix):
            raise _error("InternalError", "PutObject")
        if IfNoneMatch == "*" and Key in self.objects:
            raise _error("PreconditionFailed", "PutObject")
        self.clock += self.step
        versions = self.objects.setdefault(Key, [])
        versions.append(
            {
                "VersionId": f"v{len(versions) + 1}",
                "Body": Body,
                "Tagging": Tagging,
                "LastModified": self.clock,
            }
        )
        self.writes.append(Key)
        return {"VersionId": versions[-1]["VersionId"]}

    def delete_object(self, *, Bucket, Key):
        self.writes.append(Key)
        self.objects.pop(Key, None)

    def get_object(self, *, Bucket, Key, VersionId=None):
        self.reads.append(Key)
        if Key not in self.objects:
            raise _error(self.missing_code, "GetObject")
        versions = self.objects[Key]
        version = next((v for v in versions if v["VersionId"] == VersionId), versions[-1])
        return {"Body": io.BytesIO(version["Body"])}

    def get_paginator(self, operation: str):
        pages = {"list_objects_v2": self._objects, "list_object_versions": self._versions}
        return SimpleNamespace(paginate=pages[operation])

    def _objects(self, *, Bucket, Prefix, Delimiter=None):
        keys = sorted(key for key in self.objects if key.startswith(Prefix))
        if Delimiter:
            folders = sorted(
                {
                    f"{Prefix}{key[len(Prefix) :].split(Delimiter)[0]}{Delimiter}"
                    for key in keys
                    if Delimiter in key[len(Prefix) :]
                }
            )
            keys = [key for key in keys if Delimiter not in key[len(Prefix) :]]
            return [
                {
                    "Contents": [self._entry(key) for key in keys],
                    "CommonPrefixes": [{"Prefix": folder} for folder in folders],
                }
            ]
        return [{"Contents": [self._entry(key) for key in keys]}]

    def _entry(self, key: str) -> dict:
        return {"Key": key, "LastModified": self.objects[key][-1]["LastModified"]}

    def _versions(self, *, Bucket, Prefix):
        listed = [
            {"Key": key, "VersionId": version["VersionId"], "IsLatest": index == len(versions) - 1}
            for key, versions in sorted(self.objects.items())
            if key.startswith(Prefix)
            for index, version in enumerate(versions)
        ]
        return [{"Versions": listed}]

    def body(self, key: str) -> bytes:
        return self.objects[key][-1]["Body"]

    def tagging(self, key: str) -> str | None:
        return self.objects[key][-1]["Tagging"]


def marker_key(kind: str, value: str, tenant: str) -> str:
    digest = hashlib.sha256(f"{kind}\0{value}".encode()).hexdigest()
    return f"claims/{kind}/{digest}/{tenant}"


def test_a_republished_claim_keeps_its_first_marker_and_its_precedence() -> None:
    client = FakeS3()
    store = ClaimStore(client, "b")
    claim = Claim(SLACK_CHANNEL, "C1")
    store.create("adp", claim)
    first = client.objects[marker_key(SLACK_CHANNEL, "C1", "adp")][-1]["LastModified"]
    store.create("beta", claim)

    store.create("adp", claim)

    assert client.objects[marker_key(SLACK_CHANNEL, "C1", "adp")][-1]["LastModified"] == first
    assert store.check("adp", claim).held
    beta = store.check("beta", claim)
    assert not beta.held
    assert str(beta) == (
        "tenant adp holds an earlier claim on slack-channel C1, which a tenant on the shared app "
        "keeps for its button lifetime after it moves to another channel"
    )


def test_two_markers_with_one_last_modified_leave_the_value_to_neither() -> None:
    client = FakeS3()
    client.step = timedelta(0)
    store = ClaimStore(client, "b")
    claim = Claim(SLACK_CHANNEL, "C1")
    store.create("adp", claim)
    store.create("beta", claim)

    adp, beta = store.check("adp", claim), store.check("beta", claim)

    assert not adp.held
    assert not beta.held
    assert str(adp) == (
        "tenants adp and beta claimed slack-channel C1 at the same moment, so neither holds it"
    )


def test_own_app_channels_share_a_value_and_a_shared_app_channel_contends_with_them() -> None:
    store = ClaimStore(FakeS3(), "b")
    own, shared = Claim(SLACK_OWN_CHANNEL, "C1"), Claim(SLACK_CHANNEL, "C1")
    store.create("adp", own)
    store.create("beta", own)
    store.create("gamma", shared)

    checks = {
        tenant: store.check(tenant, claim)
        for tenant, claim in (("adp", own), ("beta", own), ("gamma", shared))
    }

    assert checks["adp"].held
    assert checks["adp"].rivals == ("gamma",)
    assert checks["beta"].held
    assert not checks["gamma"].held
    assert str(checks["gamma"]) == (
        "tenant adp holds an earlier claim on slack-channel C1, which a tenant on the shared app "
        "keeps for its button lifetime after it moves to another channel"
    )


def test_an_absent_index_is_created_without_a_read_and_a_present_one_gains_the_claims() -> None:
    client = FakeS3(missing_code="AccessDenied")
    store = ClaimStore(client, "b")
    first, second = Claim(SLACK_CHANNEL, "C1"), Claim(SLACK_APP, "A1")

    assert store.extend_index("adp", {first}).claims == frozenset()
    assert store.extend_index("adp", {second}).claims == {first}
    assert store.index("adp") == {first, second}


@pytest.mark.parametrize(
    ("kind", "written", "canonical"),
    [
        (SLACK_CHANNEL, " c0abc ", "C0ABC"),
        (SLACK_APP, "a0abc", "A0ABC"),
        (
            JIRA_PROJECT,
            "https://Acme.Atlassian.net:443/ops/adp",
            "https://acme.atlassian.net/OPS/adp",
        ),
        (JIRA_PROJECT, "https://acme.atlassian.net//OPS/", "https://acme.atlassian.net/OPS/"),
    ],
)
def test_a_value_written_by_hand_has_the_digest_of_its_canonical_form(
    kind: str, written: str, canonical: str
) -> None:
    claim = canonical_claim(kind, written)

    assert claim.digest == hashlib.sha256(f"{kind}\0{canonical}".encode()).hexdigest()
