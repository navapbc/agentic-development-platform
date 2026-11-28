"""Each tenant's Slack settings, and the tenant each shared app channel routes to."""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import scripts.aws_clients
from botocore.exceptions import ClientError
from services.integration import tenant_config
from services.integration.slack.tenant_settings import TenantSlackSettings
from services.integration.tenant_claims import CLAIMS_PROTOCOL_KEY, SLACK_CHANNEL, Claim
from services.integration.tenant_config import S3Source
from services.orchestration.webhook.tenants import TenantRouting
from tests.support import SHARED_APP_ID, SHARED_APP_YAML, hermetic_settings
from tests.unit.test_tenant_claims_store import marker_key

BUCKET = "tenant-config"
SHARED_APP = SHARED_APP_YAML
GATES = "config_version: 1\nprofile: private\n"
EARLY = datetime(2026, 11, 1, tzinfo=UTC)
LATE = datetime(2026, 11, 2, tzinfo=UTC)


def _shared(channel: str) -> str:
    return (
        "config_version: 1\n"
        "channels: [slack]\n"
        "slack:\n"
        "  app: shared\n"
        "  transport: webhook\n"
        f"  channel: {channel}\n"
        "  profile: private\n"
    )


def _own(channel: str, app_id: str) -> str:
    return (
        "config_version: 1\n"
        "channels: [slack]\n"
        "slack:\n"
        "  transport: webhook\n"
        f"  channel: {channel}\n"
        "  organization_id: T0OWN\n"
        f"  app_id: {app_id}\n"
        "  profile: private\n"
    )


class _Bucket:
    def __init__(self) -> None:
        self.files: dict[str, str] = {CLAIMS_PROTOCOL_KEY: '{"claims_protocol": 1}'}
        self.markers: dict[str, datetime] = {}
        self.reads: list[str] = []
        self.failing: set[str] = set()
        self.unreadable: dict[str, Exception] = {}

    def put(self, tenant: str, name: str, text: str) -> None:
        self.files[f"tenants/{tenant}/{name}.yaml"] = text

    def hold(self, tenant: str, channel: str, kind: str = "slack-channel", at=EARLY) -> None:
        self.markers[marker_key(kind, channel, tenant)] = at

    def get_paginator(self, operation: str) -> _Paginator:
        return _Paginator(self, operation)

    def get_object(  # noqa: N803
        self, *, Bucket: str, Key: str, VersionId: str | None = None
    ) -> dict[str, Any]:
        if Key in self.unreadable:
            raise self.unreadable[Key]
        if Key not in self.files:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        if Key.startswith("tenants/"):
            self.reads.append(Key)
        return {"Body": io.BytesIO(self.files[Key].encode())}


class _Paginator:
    def __init__(self, bucket: _Bucket, operation: str) -> None:
        self.bucket = bucket
        self.operation = operation

    def paginate(self, *, Bucket: str, Prefix: str) -> Iterator[dict[str, Any]]:  # noqa: N803
        if Prefix in self.bucket.failing:
            raise ClientError({"Error": {"Code": "SlowDown"}}, "ListObjectsV2")
        if self.operation == "list_object_versions":
            versions = [
                {
                    "Key": key,
                    "VersionId": hashlib.sha256(text.encode()).hexdigest(),
                    "IsLatest": True,
                }
                for key, text in self.bucket.files.items()
                if key.startswith(Prefix)
            ]
            yield {"Versions": versions}
            return
        contents = [
            {"Key": key, "LastModified": stamp}
            for key, stamp in self.bucket.markers.items()
            if key.startswith(Prefix)
        ]
        yield {"Contents": contents}


@pytest.fixture
def environment(tmp_path, monkeypatch) -> Path:
    env = tmp_path / "env"
    env.mkdir()
    (env / "slack-shared-app.yaml").write_text(SHARED_APP)
    monkeypatch.setenv("PLATFORM_ENV_CONFIG_DIR", str(env))
    return env


def _snapshot(bucket: _Bucket, tenants: str) -> TenantSlackSettings:
    settings = hermetic_settings(tenants=tenants, tenant_config_bucket=BUCKET)
    routing = TenantRouting(
        settings, source_factory=lambda tenant: S3Source(bucket, BUCKET, tenant)
    )
    slack = TenantSlackSettings(settings, routing, claims_client=bucket)
    slack.refresh()
    return slack


def _errors(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]


def test_shared_app_tenants_route_their_own_channels(environment) -> None:
    bucket = _Bucket()
    bucket.put("adp", "escalation", _shared("C1"))
    bucket.put("beta", "escalation", _shared("C2"))
    bucket.hold("adp", "C1")
    bucket.hold("beta", "C2")

    slack = _snapshot(bucket, "adp,beta")

    assert (slack.channel_tenant("C1"), slack.channel_tenant("C2")) == ("adp", "beta")
    assert slack.channel_tenant("C3") is None
    adp, beta = slack.slack("adp"), slack.slack("beta")
    assert adp is not None and beta is not None
    assert adp.app_id == beta.app_id == SHARED_APP_ID


@pytest.mark.parametrize(
    ("held", "holder", "reported"),
    [
        ({"beta": LATE, "adp": EARLY}, "adp", ("beta", "C1")),
        ({}, None, ("adp", "beta")),
        ({"beta": EARLY}, "beta", ()),
        ({"adp": EARLY, "beta": EARLY}, None, ()),
    ],
    ids=["earliest-marker", "no-marker", "one-marker", "tied-markers"],
)
def test_of_two_shared_tenants_naming_one_channel_only_the_deciding_marker_is_served(
    environment,
    caplog: pytest.LogCaptureFixture,
    held: dict[str, datetime],
    holder: str | None,
    reported: tuple[str, ...],
) -> None:
    bucket = _Bucket()
    for tenant in ("adp", "beta"):
        bucket.put(tenant, "escalation", _shared("C1"))
    for tenant, at in held.items():
        bucket.hold(tenant, "C1", at=at)

    slack = _snapshot(bucket, "adp,beta")

    assert slack.channel_tenant("C1") == holder
    served = {tenant for tenant in ("adp", "beta") if slack.slack(tenant) is not None}
    assert served == ({holder} if holder else set())
    if reported:
        assert any(all(word in message for word in reported) for message in _errors(caplog))


def test_without_a_bucket_two_tenants_naming_one_channel_are_both_refused(environment) -> None:
    settings = hermetic_settings(tenants="adp,beta")
    environment.joinpath("tenants", "adp").mkdir(parents=True)
    environment.joinpath("tenants", "beta").mkdir(parents=True)
    for tenant in ("adp", "beta"):
        environment.joinpath("tenants", tenant, "escalation.yaml").write_text(_shared("C1"))

    slack = TenantSlackSettings(settings, TenantRouting(settings))
    slack.refresh()

    assert (slack.slack("adp"), slack.slack("beta")) == (None, None)
    assert slack.channel_tenant("C1") is None


@pytest.mark.parametrize(
    ("first", "served", "refused"),
    [("adp", "adp", "beta"), ("beta", "beta", "adp")],
    ids=["shared-tenant-first", "own-app-tenant-first"],
)
def test_an_own_app_tenant_naming_a_shared_channel_is_a_second_claimant(
    environment, first: str, served: str, refused: str
) -> None:
    bucket = _Bucket()
    bucket.put("adp", "escalation", _shared("C1"))
    bucket.put("beta", "escalation", _own("C1", "A0BETA"))
    kinds = {"adp": "slack-channel", "beta": "slack-own-channel"}
    for tenant, kind in kinds.items():
        bucket.markers[marker_key(kind, "C1", tenant)] = EARLY if tenant == first else LATE

    slack = _snapshot(bucket, "adp,beta")

    assert slack.slack(served) is not None
    assert slack.slack(refused) is None
    assert slack.channel_tenant("C1") == ("adp" if served == "adp" else None)


def test_own_app_tenants_may_name_one_channel(environment) -> None:
    bucket = _Bucket()
    bucket.put("adp", "escalation", _own("C1", "A0ADP"))
    bucket.put("beta", "escalation", _own("C1", "A0BETA"))

    slack = _snapshot(bucket, "adp,beta")

    assert slack.slack("adp") is not None
    assert slack.slack("beta") is not None
    assert slack.channel_tenant("C1") is None


def test_the_webhook_and_the_poster_refuse_one_channel_alike_whatever_secrets_they_hold(
    environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    bucket = _Bucket()
    bucket.put("a", "escalation", _shared("C1"))
    bucket.put("b", "escalation", _own("C1", "A0B"))
    bucket.put("c", "escalation", _own("C2", "A0C"))
    bucket.hold("b", "C1", "slack-own-channel", EARLY)
    bucket.hold("a", "C1", "slack-channel", LATE)
    monkeypatch.setenv("SLACK_SIGNING_SECRET_B", "shared-by-b-and-c")
    monkeypatch.setenv("SLACK_SIGNING_SECRET_C", "shared-by-b-and-c")
    webhook = _snapshot(bucket, "a,b,c")
    monkeypatch.delenv("SLACK_SIGNING_SECRET_B")
    monkeypatch.delenv("SLACK_SIGNING_SECRET_C")
    poster = _snapshot(bucket, "a,b,c")

    for snapshot in (webhook, poster):
        assert snapshot.slack("a") is None
        assert snapshot.channel_tenant("C1") is None
    assert webhook.slack("b") is None
    assert poster.slack("b") is not None


def test_a_shared_tenant_without_a_marker_on_its_channel_is_refused(
    environment, caplog: pytest.LogCaptureFixture
) -> None:
    bucket = _Bucket()
    bucket.put("adp", "escalation", _shared("C1"))

    slack = _snapshot(bucket, "adp")

    assert slack.slack("adp") is None
    assert slack.channel_tenant("C1") is None
    assert any("adp" in message and "C1" in message for message in _errors(caplog))


def test_a_refresh_fetches_only_the_files_whose_version_changed(environment) -> None:
    bucket = _Bucket()
    bucket.put("adp", "escalation", _shared("C1"))
    bucket.put("adp", "gates", GATES)
    bucket.hold("adp", "C1")
    slack = _snapshot(bucket, "adp")
    bucket.reads.clear()

    slack.refresh()
    assert bucket.reads == []

    bucket.put("adp", "gates", GATES + "# reviewed\n")
    slack.refresh()
    assert bucket.reads == ["tenants/adp/gates.yaml"]


def test_a_changed_environment_gates_file_reparses_every_tenant_inheriting_it(
    environment,
) -> None:
    (environment / "gates.yaml").write_text("config_version: 1\nprofile: government\n")
    bucket = _Bucket()
    for tenant, channel in (("adp", "C1"), ("beta", "C2")):
        bucket.put(tenant, "escalation", _shared(channel))
        bucket.hold(tenant, channel)
    slack = _snapshot(bucket, "adp,beta")
    assert (slack.slack("adp"), slack.slack("beta")) == (None, None)

    (environment / "gates.yaml").write_text(GATES)
    slack.refresh()

    assert slack.slack("adp") is not None
    assert slack.slack("beta") is not None


def test_a_failed_listing_at_first_start_refuses_only_that_channels_claimants(
    environment,
) -> None:
    bucket = _Bucket()
    bucket.put("adp", "escalation", _shared("C1"))
    bucket.put("beta", "escalation", _shared("C2"))
    bucket.hold("adp", "C1")
    bucket.hold("beta", "C2")
    bucket.failing.add(Claim(SLACK_CHANNEL, "C1").prefix)

    slack = _snapshot(bucket, "adp,beta")

    assert slack.slack("adp") is None
    assert slack.channel_tenant("C1") is None
    assert slack.channel_tenant("C2") == "beta"


def test_a_failed_listing_after_a_good_refresh_keeps_the_channels_last_decision(
    environment,
) -> None:
    bucket = _Bucket()
    bucket.put("adp", "escalation", _shared("C1"))
    bucket.hold("adp", "C1")
    slack = _snapshot(bucket, "adp")

    bucket.failing.add(Claim(SLACK_CHANNEL, "C1").prefix)
    slack.refresh()

    assert slack.slack("adp") is not None
    assert slack.channel_tenant("C1") == "adp"


def test_a_failed_listing_refuses_the_channel_once_its_claimants_change(environment) -> None:
    bucket = _Bucket()
    bucket.put("adp", "escalation", _shared("C1"))
    bucket.hold("adp", "C1")
    slack = _snapshot(bucket, "adp,beta")
    assert slack.channel_tenant("C1") == "adp"

    bucket.failing.add(Claim(SLACK_CHANNEL, "C1").prefix)
    bucket.put("beta", "escalation", _shared("C1"))
    slack.refresh()

    assert slack.channel_tenant("C1") is None
    assert (slack.slack("adp"), slack.slack("beta")) == (None, None)


def test_a_malformed_shared_app_keeps_the_last_app_and_still_updates_own_app_tenants(
    environment,
) -> None:
    bucket = _Bucket()
    bucket.put("adp", "escalation", _shared("C1"))
    bucket.put("beta", "escalation", _own("C5", "A0BETA"))
    bucket.hold("adp", "C1")
    slack = _snapshot(bucket, "adp,beta")

    (environment / "slack-shared-app.yaml").write_text("config_version: 1\napp_id: [\n")
    bucket.put("beta", "escalation", _own("C6", "A0BETA"))
    slack.refresh()

    shared_app = slack.shared_app()
    assert shared_app is not None and shared_app.app_id == SHARED_APP_ID
    assert slack.channel_tenant("C1") == "adp"
    beta = slack.slack("beta")
    assert beta is not None and beta.channel == "C6"


def test_a_bundle_listing_that_fails_at_first_start_is_served_after_the_next_refresh(
    environment,
) -> None:
    bucket = _Bucket()
    bucket.put("adp", "escalation", _shared("C1"))
    bucket.hold("adp", "C1")
    bucket.failing.add("tenants/adp/")
    slack = _snapshot(bucket, "adp")
    assert slack.channel_tenant("C1") is None

    bucket.failing.clear()
    slack.refresh()

    assert slack.channel_tenant("C1") == "adp"


async def test_a_first_refresh_that_raises_still_starts_the_interval_refresh(environment) -> None:
    attempts: list[str] = []

    class _Unreachable:
        def tenants(self) -> list[str]:
            attempts.append("tenants")
            raise ConnectionError("the config bucket is unreachable")

        def source(self, tenant: str) -> Any:
            raise AssertionError(tenant)

    settings = hermetic_settings(tenants="adp", tenant_config_refresh_seconds=0)
    slack = TenantSlackSettings(settings, _Unreachable())

    await slack.refresh_then_start()
    for _ in range(100):
        if len(attempts) > 1:
            break
        await asyncio.sleep(0.01)
    for task in asyncio.all_tasks() - {asyncio.current_task()}:
        task.cancel()

    assert len(attempts) > 1


@pytest.mark.parametrize(
    "error",
    [None, ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")],
    ids=["absent", "access-denied"],
)
def test_without_the_claims_protocol_object_shared_tenants_are_refused_and_own_app_served(
    environment, caplog: pytest.LogCaptureFixture, error: Exception | None
) -> None:
    bucket = _Bucket()
    del bucket.files[CLAIMS_PROTOCOL_KEY]
    if error is not None:
        bucket.unreadable[CLAIMS_PROTOCOL_KEY] = error
    bucket.put("adp", "escalation", _shared("C1"))
    bucket.put("beta", "escalation", _own("C5", "A0BETA"))
    bucket.hold("adp", "C1")

    slack = _snapshot(bucket, "adp,beta")
    slack.refresh()

    assert slack.slack("adp") is None
    assert slack.channel_tenant("C1") is None
    assert slack.slack("beta") is not None
    assert len([m for m in _errors(caplog) if "claims-protocol.json" in m]) == 1


def test_a_failed_claims_protocol_read_keeps_the_last_answer_and_refuses_at_first_start(
    environment,
) -> None:
    bucket = _Bucket()
    bucket.put("adp", "escalation", _shared("C1"))
    bucket.hold("adp", "C1")
    served = _snapshot(bucket, "adp")
    bucket.unreadable[CLAIMS_PROTOCOL_KEY] = TimeoutError("read timed out")

    served.refresh()
    first_start = _snapshot(bucket, "adp")

    assert served.channel_tenant("C1") == "adp"
    assert first_start.slack("adp") is None
    assert first_start.channel_tenant("C1") is None


def test_a_refresh_over_five_tenants_builds_one_s3_client_that_gives_up_within_a_minute(
    environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.slack_poster.server import tenant_slack_settings

    bucket = _Bucket()
    names = ["t1", "t2", "t3", "t4", "t5"]
    for index, tenant in enumerate(names):
        bucket.put(tenant, "escalation", _shared(f"C{index}"))
        bucket.hold(tenant, f"C{index}")
    clients: list[dict[str, Any]] = []

    def s3_client(**kwargs: Any) -> _Bucket:
        clients.append(kwargs)
        return bucket

    monkeypatch.setattr(scripts.aws_clients, "s3_client", s3_client)
    tenant_config.reset()
    settings = hermetic_settings(tenants=",".join(names), tenant_config_bucket=BUCKET)

    slack = tenant_slack_settings(settings)
    slack.refresh()

    [client] = clients
    config = client["config"]
    assert config.connect_timeout + config.read_timeout < 60
    assert config.retries["mode"] == "standard"
    assert [slack.channel_tenant(f"C{index}") for index in range(5)] == names
    tenant_config.reset()


def test_a_shared_tenants_leftover_signing_secret_does_not_refuse_it(
    environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    bucket = _Bucket()
    bucket.put("adp", "escalation", _shared("C1"))
    bucket.put("beta", "escalation", _own("C5", "A0BETA"))
    bucket.hold("adp", "C1")
    monkeypatch.setenv("SLACK_SIGNING_SECRET_ADP", "retired-app-secret")
    monkeypatch.setenv("SLACK_SIGNING_SECRET_BETA", "retired-app-secret")

    slack = _snapshot(bucket, "adp,beta")

    assert slack.slack("adp") is not None
    assert slack.channel_tenant("C1") == "adp"
    assert slack.slack("beta") is not None


@pytest.mark.parametrize(
    "held_as",
    ["key", "fingerprint", "key-and-fingerprint"],
    ids=["webhook-keys", "posting-service-fingerprints", "key-against-fingerprint"],
)
def test_two_tenants_holding_one_action_key_are_both_refused(
    environment,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    held_as: str,
) -> None:
    bucket = _Bucket()
    for tenant, channel in (("adp", "C1"), ("beta", "C2"), ("gamma", "C3")):
        bucket.put(tenant, "escalation", _shared(channel))
        bucket.hold(tenant, channel)
    keys = {"ADP": "one-key", "BETA": "one-key", "GAMMA": "gamma-key"}
    for index, (tenant, key) in enumerate(keys.items()):
        as_key = held_as == "key" or (held_as == "key-and-fingerprint" and index == 0)
        if as_key:
            monkeypatch.setenv(f"ESCALATION_ACTION_KEY_{tenant}", key)
        else:
            monkeypatch.setenv(
                f"ESCALATION_ACTION_KEY_SHA256_{tenant}", hashlib.sha256(key.encode()).hexdigest()
            )

    slack = _snapshot(bucket, "adp,beta,gamma")

    assert (slack.slack("adp"), slack.slack("beta")) == (None, None)
    assert slack.slack("gamma") is not None
    assert any("adp, beta" in message and "action key" in message for message in _errors(caplog))


def test_a_shared_tenant_without_a_key_or_fingerprint_is_served_and_named_once(
    environment, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bucket = _Bucket()
    for tenant, channel in (("adp", "C1"), ("beta", "C2")):
        bucket.put(tenant, "escalation", _shared(channel))
        bucket.hold(tenant, channel)
    monkeypatch.setenv("ESCALATION_ACTION_KEY_SHA256_ADP", "a" * 64)

    slack = _snapshot(bucket, "adp,beta")
    slack.refresh()

    assert slack.slack("adp") is not None and slack.slack("beta") is not None
    named = [message for message in _errors(caplog) if "tenants beta have no" in message]
    assert len(named) == 1


def test_an_own_app_tenant_holding_the_shared_app_id_is_refused(environment) -> None:
    shared_app = environment / "slack-shared-app.yaml"
    shared_app.unlink()
    bucket = _Bucket()
    bucket.put("adp", "escalation", _own("C1", SHARED_APP_ID))
    slack = _snapshot(bucket, "adp")
    assert slack.slack("adp") is not None

    shared_app.write_text(SHARED_APP)
    slack.refresh()

    assert slack.slack("adp") is None


def test_a_refused_escalation_file_keeps_the_last_valid_channel(environment) -> None:
    bucket = _Bucket()
    bucket.put("adp", "escalation", _shared("C1"))
    bucket.hold("adp", "C1")
    slack = _snapshot(bucket, "adp")

    bucket.put("adp", "escalation", _shared("G1"))
    slack.refresh()

    assert slack.channel_tenant("C1") == "adp"
    assert slack.channel_tenant("G1") is None


def test_a_refresh_reads_only_the_escalation_and_gates_files(environment) -> None:
    bucket = _Bucket()
    bucket.put("adp", "escalation", _shared("C1"))
    bucket.put("adp", "gates", GATES)
    bucket.put("adp", "jira", "config_version: 1\n")
    bucket.put("adp", "repositories", "config_version: 1\n")
    bucket.hold("adp", "C1")

    slack = _snapshot(bucket, "adp")

    assert slack.channel_tenant("C1") == "adp"
    assert sorted(bucket.reads) == ["tenants/adp/escalation.yaml", "tenants/adp/gates.yaml"]


def _left_c1(bucket: _Bucket) -> None:
    bucket.put("adp", "escalation", _shared("C3"))
    bucket.hold("adp", "C1")
    bucket.hold("adp", "C3")


async def test_a_left_channel_routes_to_the_tenant_retaining_it_until_the_next_refresh(
    environment,
) -> None:
    bucket = _Bucket()
    _left_c1(bucket)
    slack = _snapshot(bucket, "adp")

    assert await slack.retained_holder("c1") == "adp"
    bucket.failing.add(Claim(SLACK_CHANNEL, "C1").prefix)
    assert await slack.retained_holder("C1") == "adp"

    slack.refresh()

    assert await slack.retained_holder("C1") is None


@pytest.mark.parametrize(
    "case",
    ["no-claims-protocol", "listing-fails", "earlier-unserved-holder", "channel-still-named"],
)
async def test_a_left_channel_is_refused_when_its_holder_cannot_be_trusted(
    environment, caplog: pytest.LogCaptureFixture, case: str
) -> None:
    bucket = _Bucket()
    _left_c1(bucket)
    if case == "no-claims-protocol":
        del bucket.files[CLAIMS_PROTOCOL_KEY]
    elif case == "listing-fails":
        bucket.failing.add(Claim(SLACK_CHANNEL, "C1").prefix)
    elif case == "earlier-unserved-holder":
        bucket.hold("beta", "C1", at=datetime(2026, 10, 1, tzinfo=UTC))
    else:
        bucket.put("adp", "escalation", _shared("C1"))
    slack = _snapshot(bucket, "adp")

    with caplog.at_level(logging.WARNING):
        assert await slack.retained_holder("C1") is None

    if case == "listing-fails":
        assert any("could not be listed for a click" in r.getMessage() for r in caplog.records)
