"""A publish claims the bundle's values before it uploads, and an admin repairs the claims."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import scripts.config
from botocore.exceptions import ClientError
from scripts import tenant_config as tenant_config_command
from services.integration import tenant_config
from services.integration.credentials import ConfigError
from services.integration.tenant_claims import (
    CLAIMS_PROTOCOL_KEY,
    JIRA_PROJECT,
    SLACK_APP,
    SLACK_CHANNEL,
    Claim,
    ClaimRefusedError,
    ClaimStore,
)
from tests.support import hermetic_settings, write_image_policy
from tests.unit.test_tenant_claims import (
    SHARED_APP,
    _directory,
    _jira,
    _own_slack,
    _shared_slack,
)
from tests.unit.test_tenant_claims_store import FakeS3, marker_key
from tests.unit.test_tenant_config import GATES, REPOSITORIES, TICKETS

OPS = "https://acme.atlassian.net/OPS/adp"
T0 = datetime(2026, 11, 1, tzinfo=UTC)
TWO_REFRESHES = timedelta(seconds=120)


@pytest.fixture
def environment(tmp_path, monkeypatch) -> Iterator[Path]:
    env = _directory(tmp_path / "env", {"gates.yaml": GATES, "slack-shared-app.yaml": SHARED_APP})
    write_image_policy(env)
    _directory(
        env / "tenants" / "adp",
        {
            "repositories.yaml": REPOSITORIES,
            "tickets.yaml": TICKETS,
            "escalation.yaml": _shared_slack("C9"),
        },
    )
    monkeypatch.setenv("PLATFORM_ENV_CONFIG_DIR", str(env))
    base = hermetic_settings(tenant="adp", ticket_backend="rebar")
    monkeypatch.setattr(scripts.config, "get_settings", lambda: base)
    tenant_config.reset()
    yield env
    tenant_config.reset()


def _settings(tenant: str):
    return hermetic_settings(tenant=tenant, ticket_backend="rebar")


def _bundle(tmp_path: Path, tenant: str, channel: str) -> Path:
    return _directory(
        tmp_path / tenant,
        {
            "repositories.yaml": REPOSITORIES,
            "tickets.yaml": TICKETS,
            "escalation.yaml": _shared_slack(channel),
        },
    )


def _held(client: FakeS3, tenant: str, *values: str) -> None:
    store = ClaimStore(client, "b")
    claims = {Claim(SLACK_CHANNEL, value) for value in values}
    store.extend_index(tenant, claims)
    for claim in claims:
        store.create(tenant, claim)


def _publish_files(client: FakeS3, tenant: str, files: dict[str, str]) -> None:
    for name, text in files.items():
        client.put_object(Bucket="b", Key=f"tenants/{tenant}/{name}", Body=text.encode())


def test_a_publish_naming_a_value_another_tenant_holds_uploads_nothing_and_leaves_no_marker(
    environment, tmp_path
) -> None:
    client = FakeS3()
    _held(client, "adp", "C1")

    with pytest.raises(
        ClaimRefusedError, match="tenant adp holds an earlier claim on slack-channel C1"
    ):
        tenant_config_command.publish(
            _bundle(tmp_path, "beta", "C1"), _settings("beta"), "b", client
        )

    assert not [key for key in client.objects if key.startswith("tenants/beta/")]
    assert marker_key(SLACK_CHANNEL, "C1", "beta") not in client.objects


def test_the_validate_probe_reports_a_value_another_tenant_holds_without_writing(
    environment, tmp_path, capsys
) -> None:
    client = FakeS3()
    _held(client, "adp", "C1")
    writes = list(client.writes)

    valid = tenant_config_command.validate(
        _bundle(tmp_path, "beta", "C1"), _settings("beta"), bucket="b", client=client
    )

    assert valid is False
    assert "tenant adp holds an earlier claim on slack-channel C1" in capsys.readouterr().err
    assert client.writes == writes


def test_validate_without_a_bucket_skips_the_probe_and_returns_the_offline_result(
    environment, tmp_path, capsys
) -> None:
    valid = tenant_config_command.validate(_bundle(tmp_path, "beta", "C1"), _settings("beta"))

    assert valid is True
    assert "skipped the claims probe" in capsys.readouterr().out


def test_validate_skips_the_probe_when_the_named_aws_profile_does_not_exist(
    environment, tmp_path, capsys, monkeypatch
) -> None:
    monkeypatch.setenv("AWS_PROFILE", "profile-the-runner-lacks")
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_WEB_IDENTITY_TOKEN_FILE"):
        monkeypatch.delenv(name, raising=False)

    valid = tenant_config_command.validate(
        _bundle(tmp_path, "beta", "C1"), _settings("beta"), bucket="b"
    )

    assert valid is True
    assert "skipped the claims probe" in capsys.readouterr().out


def test_a_probe_the_bucket_refuses_fails_validation_naming_the_error_code(
    environment, tmp_path, capsys
) -> None:
    class DeniedS3(FakeS3):
        def get_paginator(self, name):
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "ListObjectsV2")

    valid = tenant_config_command.validate(
        _bundle(tmp_path, "beta", "C1"), _settings("beta"), bucket="b", client=DeniedS3()
    )

    assert valid is False
    assert "the claims probe failed with AccessDenied" in capsys.readouterr().err


def test_a_publish_claims_uploads_with_the_protocol_tag_and_releases_what_it_dropped(
    environment, tmp_path
) -> None:
    client = FakeS3()
    _held(client, "adp", "C8")
    old_app = Claim(SLACK_APP, "A0OLD")
    store = ClaimStore(client, "b")
    store.extend_index("adp", {old_app})
    store.create("adp", old_app)
    _publish_files(client, "adp", {"checks.yaml": "old"})

    versions = tenant_config_command.publish(
        _bundle(tmp_path, "adp", "C1"), _settings("adp"), "b", client
    )

    assert versions == {"escalation": "v1", "repositories": "v1", "tickets": "v1"}
    assert client.tagging("tenants/adp/escalation.yaml") == "claims-protocol=1"
    assert "tenants/adp/checks.yaml" not in client.objects
    assert marker_key(SLACK_CHANNEL, "C1", "adp") in client.objects
    assert marker_key(SLACK_APP, "A0OLD", "adp") not in client.objects
    assert marker_key(SLACK_CHANNEL, "C8", "adp") in client.objects
    index = store.read_index("adp")
    assert index.claims == {Claim(SLACK_CHANNEL, "C1")}
    assert set(index.retained) == {Claim(SLACK_CHANNEL, "C8")}


def test_the_holder_publishes_and_warns_of_a_marker_written_outside_the_protocol(
    environment, tmp_path, capsys
) -> None:
    client = FakeS3()
    _held(client, "adp", "C1")
    client.put_object(Bucket="b", Key=marker_key(SLACK_CHANNEL, "C1", "beta"), Body=b"")

    tenant_config_command.publish(_bundle(tmp_path, "adp", "C1"), _settings("adp"), "b", client)

    assert "tenant beta also claims slack-channel C1" in capsys.readouterr().err
    assert client.tagging("tenants/adp/escalation.yaml") == "claims-protocol=1"


def test_an_upload_that_fails_keeps_the_claims_and_a_rerun_completes(environment, tmp_path) -> None:
    client = FakeS3()
    _held(client, "adp", "C8")
    bundle = _bundle(tmp_path, "adp", "C1")
    client.failing_prefix = "tenants/"

    with pytest.raises(ClientError):
        tenant_config_command.publish(bundle, _settings("adp"), "b", client)

    store = ClaimStore(client, "b")
    assert store.index("adp") == {Claim(SLACK_CHANNEL, "C1"), Claim(SLACK_CHANNEL, "C8")}
    assert marker_key(SLACK_CHANNEL, "C1", "adp") in client.objects
    assert marker_key(SLACK_CHANNEL, "C8", "adp") in client.objects

    client.failing_prefix = ""
    tenant_config_command.publish(bundle, _settings("adp"), "b", client)

    assert store.index("adp") == {Claim(SLACK_CHANNEL, "C1")}
    assert set(store.read_index("adp").retained) == {Claim(SLACK_CHANNEL, "C8")}


def test_the_backfill_claims_every_value_but_one_two_bundles_name_and_fails(
    environment, capsys
) -> None:
    for tenant in ("adp", "beta"):
        (environment / "tenants").mkdir(exist_ok=True)
        (environment / "tenants" / f"{tenant}.env").write_text("TICKET_BACKEND=jira\n")
    client = FakeS3()
    _publish_files(
        client, "adp", {"escalation.yaml": _shared_slack("C1"), "jira.yaml": _jira("  {}")}
    )
    beta_jira = _jira("  PLAT:\n    intake_label: plat-beta")
    _publish_files(client, "beta", {"escalation.yaml": _shared_slack("C2"), "jira.yaml": beta_jira})

    code = tenant_config_command.backfill(_settings(""), "b", client)

    assert code == 1
    assert f"tenants adp and beta both claim jira-project {OPS}" in capsys.readouterr().err
    assert not [key for key in client.objects if key.startswith(Claim(JIRA_PROJECT, OPS).prefix)]
    assert ClaimStore(client, "b").index("adp") == {
        Claim(SLACK_CHANNEL, "C1"),
        Claim(JIRA_PROJECT, "https://acme.atlassian.net/PLAT/adp"),
    }
    for tenant, kind, value in (
        ("adp", SLACK_CHANNEL, "C1"),
        ("adp", JIRA_PROJECT, "https://acme.atlassian.net/PLAT/adp"),
        ("beta", SLACK_CHANNEL, "C2"),
        ("beta", JIRA_PROJECT, "https://acme.atlassian.net/PLAT/plat-beta"),
    ):
        assert marker_key(kind, value, tenant) in client.objects


@pytest.mark.parametrize("command", ["report", "backfill"])
def test_a_marker_no_published_bundle_names_is_reported_and_a_squat_is_flagged(
    environment, capsys, command: str
) -> None:
    client = FakeS3()
    _held(client, "adp", "C1")
    _held(client, "beta", "C1")
    _held(client, "gamma", "C7")
    _publish_files(client, "adp", {"escalation.yaml": _shared_slack("C1")})
    _publish_files(client, "beta", {"escalation.yaml": _shared_slack("C2")})

    code = getattr(tenant_config_command, command)(_settings(""), "b", client)

    err = capsys.readouterr().err
    assert code == 1
    assert (
        "tenant beta holds a marker on slack-channel C1 that no published bundle of beta names, "
        "a squat on a value the published bundle of tenant adp names"
    ) in err
    assert (
        "tenant gamma holds a marker on slack-channel C7 that no published bundle of gamma names\n"
        in err
    )
    assert "tenant adp holds a marker" not in err


def test_the_report_reads_each_tenants_ticket_backend_without_the_tenant_running_it(
    environment, capsys
) -> None:
    (environment / "tenants" / "adp.env").write_text("TICKET_BACKEND=jira\n")
    client = FakeS3()
    jira = _jira("  {}")
    _publish_files(client, "adp", {"escalation.yaml": _shared_slack("C1"), "jira.yaml": jira})
    _publish_files(client, "beta", {"escalation.yaml": _shared_slack("C2"), "jira.yaml": jira})
    store = ClaimStore(client, "b")
    store.extend_index("adp", {Claim(SLACK_CHANNEL, "C1"), Claim(JIRA_PROJECT, OPS)})
    for claim in (Claim(SLACK_CHANNEL, "C1"), Claim(JIRA_PROJECT, OPS)):
        store.create("adp", claim)
    _held(client, "beta", "C2")

    code = tenant_config_command.report(
        hermetic_settings(tenant="adp", ticket_backend="jira"), "b", client
    )

    assert code == 0, capsys.readouterr().err
    assert "jira-project" not in capsys.readouterr().err


def test_the_report_reads_only_the_files_that_hold_claims(environment) -> None:
    client = FakeS3()
    _publish_files(
        client,
        "adp",
        {"escalation.yaml": _shared_slack("C1"), "jira.yaml": _jira("  {}"), "gates.yaml": GATES},
    )
    client.put_object(Bucket="b", Key="tenants/adp/archive/old.yaml", Body=b"")

    claims = tenant_config_command._published_claims(_settings(""), "b", client)

    assert claims == {"adp": {Claim(SLACK_CHANNEL, "C1")}}
    assert sorted(key for key in client.reads if key.startswith("tenants/")) == [
        "tenants/adp/escalation.yaml",
        "tenants/adp/jira.yaml",
    ]


def test_a_publish_validates_and_uploads_with_one_client(
    environment, tmp_path, monkeypatch
) -> None:
    import boto3

    clients: list[FakeS3] = []

    def client(settings) -> FakeS3:
        clients.append(FakeS3())
        return clients[-1]

    monkeypatch.setattr(tenant_config_command, "_s3_client", client)
    monkeypatch.setattr(boto3, "Session", lambda: type("S", (), {"get_credentials": object})())
    bundle = _bundle(tmp_path, "beta", "C1")

    code = tenant_config_command.main(
        ["publish", "--tenant", "beta", "--dir", str(bundle), "--bucket", "b"]
    )

    assert code == 0
    assert len(clients) == 1


def test_validate_refuses_a_tenant_only_file_in_the_platform_directory(
    environment, monkeypatch, tmp_path, capsys
) -> None:
    platform = tmp_path / "platform"
    platform.mkdir()
    (platform / "escalation.yaml").write_text("config_version: 1\n")
    monkeypatch.setenv("PLATFORM_ENV_CONFIG_DIR", str(environment))

    code = tenant_config_command.main(
        [
            "validate",
            "--tenant",
            "beta",
            "--dir",
            str(_bundle(tmp_path, "beta", "C1")),
            "--platform-dir",
            str(platform),
        ]
    )

    assert code == 1
    assert "escalation.yaml, which only a tenant's bundle holds" in capsys.readouterr().err


@pytest.mark.parametrize("missing_code", ["NoSuchKey", "AccessDenied"])
def test_a_shared_app_publish_without_the_claims_protocol_object_writes_nothing(
    environment, tmp_path, missing_code: str
) -> None:
    client = FakeS3(missing_code, claims_protocol=False)

    with pytest.raises(ConfigError, match="claims-protocol.json is absent .* or not readable"):
        tenant_config_command.publish(
            _bundle(tmp_path, "beta", "C1"), _settings("beta"), "b", client
        )

    assert client.writes == []


def test_a_shared_app_publish_succeeds_once_the_claims_protocol_object_exists(
    environment, tmp_path
) -> None:
    client = FakeS3(claims_protocol=False)
    bundle = _bundle(tmp_path, "beta", "C1")
    with pytest.raises(ConfigError):
        tenant_config_command.publish(bundle, _settings("beta"), "b", client)

    client.put_object(Bucket="b", Key=CLAIMS_PROTOCOL_KEY, Body=b"{}")
    tenant_config_command.publish(bundle, _settings("beta"), "b", client)

    assert ClaimStore(client, "b").index("beta") == {Claim(SLACK_CHANNEL, "C1")}


def test_an_own_app_publish_needs_no_claims_protocol_object(environment, tmp_path) -> None:
    client = FakeS3(claims_protocol=False)
    bundle = _directory(tmp_path / "beta", {"escalation.yaml": _own_slack("C1", "A0BETA")})

    tenant_config_command.publish(bundle, _settings("beta"), "b", client)

    assert "tenants/beta/escalation.yaml" in client.objects


def test_validate_refuses_a_shared_app_bundle_without_the_claims_protocol_object(
    environment, tmp_path, capsys
) -> None:
    valid = tenant_config_command.validate(
        _bundle(tmp_path, "beta", "C1"),
        _settings("beta"),
        bucket="b",
        client=FakeS3("AccessDenied", claims_protocol=False),
    )

    assert valid is False
    assert "claims-protocol.json is absent" in capsys.readouterr().err


def test_a_release_removes_one_tenants_marker_on_one_value_and_its_index_entry(
    environment,
) -> None:
    client = FakeS3()
    _held(client, "adp", "C1", "C2")
    _held(client, "beta", "C1")

    tenant_config_command.release(_settings("adp"), "b", SLACK_CHANNEL, "c1", client)

    assert marker_key(SLACK_CHANNEL, "C1", "adp") not in client.objects
    assert marker_key(SLACK_CHANNEL, "C1", "beta") in client.objects
    assert marker_key(SLACK_CHANNEL, "C2", "adp") in client.objects
    assert ClaimStore(client, "b").index("adp") == {Claim(SLACK_CHANNEL, "C2")}


def _shared_with_lifetime(channel: str, hours: int) -> str:
    return _shared_slack(channel).replace(
        "  profile: private\n", f"  profile: private\n  token_ttl_hours: {hours}\n"
    )


def _publish_at(tmp_path: Path, tenant: str, escalation: str, client: FakeS3, now: datetime):
    bundle = _directory(tmp_path / tenant, {"escalation.yaml": escalation})
    return tenant_config_command.publish(bundle, _settings(tenant), "b", client, now=now)


def test_a_channel_a_shared_tenant_leaves_stays_claimed_through_its_button_lifetime(
    environment, tmp_path
) -> None:
    client = FakeS3()
    _publish_at(tmp_path, "adp", _shared_slack("C1"), client, T0)
    moved = T0 + timedelta(hours=1)

    _publish_at(tmp_path, "adp", _shared_slack("C2"), client, moved)

    assert marker_key(SLACK_CHANNEL, "C1", "adp") in client.objects
    assert ClaimStore(client, "b").read_index("adp").retained == {
        Claim(SLACK_CHANNEL, "C1"): moved + timedelta(hours=72) + TWO_REFRESHES
    }
    with pytest.raises(ClaimRefusedError, match="tenant adp holds an earlier claim on .* C1"):
        _publish_at(tmp_path, "beta", _shared_slack("C1"), client, moved)
    assert marker_key(SLACK_CHANNEL, "C1", "beta") not in client.objects


def test_a_refused_publish_leaves_no_refused_claim_in_the_index_to_retain_later(
    environment, tmp_path
) -> None:
    client = FakeS3()
    _publish_at(tmp_path, "adp", _shared_slack("C1"), client, T0)
    _publish_at(tmp_path, "beta", _shared_slack("C2"), client, T0)

    with pytest.raises(ClaimRefusedError):
        _publish_at(tmp_path, "beta", _shared_slack("C1"), client, T0)
    assert ClaimStore(client, "b").read_index("beta").claims == {Claim(SLACK_CHANNEL, "C2")}

    _publish_at(tmp_path, "beta", _shared_slack("C3"), client, T0)

    assert ClaimStore(client, "b").read_index("beta").retained == {
        Claim(SLACK_CHANNEL, "C2"): T0 + timedelta(hours=72) + TWO_REFRESHES
    }


def test_the_first_publish_past_the_release_time_releases_the_retained_channel(
    environment, tmp_path
) -> None:
    client = FakeS3()
    _publish_at(tmp_path, "adp", _shared_slack("C1"), client, T0)
    _publish_at(tmp_path, "adp", _shared_slack("C2"), client, T0)

    _publish_at(
        tmp_path, "adp", _shared_slack("C2"), client, T0 + timedelta(hours=72) + TWO_REFRESHES
    )

    assert marker_key(SLACK_CHANNEL, "C1", "adp") not in client.objects
    assert ClaimStore(client, "b").read_index("adp").retained == {}


@pytest.mark.parametrize(("previous", "new"), [(168, 24), (24, 168)])
def test_the_retention_follows_the_button_lifetime_of_the_replaced_bundle(
    environment, tmp_path, previous: int, new: int
) -> None:
    client = FakeS3()
    _publish_at(tmp_path, "adp", _shared_with_lifetime("C1", previous), client, T0)

    _publish_at(tmp_path, "adp", _shared_with_lifetime("C2", new), client, T0)

    assert ClaimStore(client, "b").read_index("adp").retained == {
        Claim(SLACK_CHANNEL, "C1"): T0 + timedelta(hours=previous) + TWO_REFRESHES
    }


class _PublishedBundle:
    def __init__(self, files: dict[str, str]) -> None:
        self.files = files

    def latest(self) -> str:
        return "v1"

    def fetch_files(self, identity: str, names: list[str]) -> dict[str, str]:
        return {name: self.files[name] for name in names if name in self.files}


def _escalation_with_lifetime(value: str) -> str:
    return _shared_slack("C1").replace(
        "  profile: private\n", f"  profile: private\n  token_ttl_hours: {value}\n"
    )


@pytest.mark.parametrize(
    ("published", "hours"),
    [
        ({"escalation": _escalation_with_lifetime("12")}, 12),
        ({}, 72),
        ({"escalation": _escalation_with_lifetime("0")}, 72),
        ({"escalation": _escalation_with_lifetime("-3")}, 72),
        ({"escalation": _escalation_with_lifetime("'48'")}, 72),
        ({"escalation": _escalation_with_lifetime("true")}, 72),
        ({"escalation": "slack: [unclosed\n"}, 72),
        ({"escalation": "- a list\n"}, 72),
    ],
    ids=[
        "published-file",
        "no-file",
        "zero",
        "negative",
        "string",
        "boolean",
        "invalid-yaml",
        "not-a-mapping",
    ],
)
def test_the_replaced_button_lifetime_falls_back_to_the_default(
    published: dict[str, str], hours: int
) -> None:
    lifetime = tenant_config_command._published_token_ttl_hours(_PublishedBundle(published))

    assert lifetime == hours


def test_a_channel_named_again_inside_its_window_keeps_its_first_marker(
    environment, tmp_path
) -> None:
    client = FakeS3()
    _publish_at(tmp_path, "adp", _shared_slack("C1"), client, T0)
    first = client.objects[marker_key(SLACK_CHANNEL, "C1", "adp")][-1]["LastModified"]
    _publish_at(tmp_path, "adp", _shared_slack("C2"), client, T0)
    _publish_at(tmp_path, "adp", _shared_slack("C1"), client, T0 + timedelta(hours=1))

    _publish_at(tmp_path, "adp", _shared_slack("C1"), client, T0 + timedelta(hours=80))

    assert client.objects[marker_key(SLACK_CHANNEL, "C1", "adp")][-1]["LastModified"] == first
    assert ClaimStore(client, "b").index("adp") == {Claim(SLACK_CHANNEL, "C1")}


def test_the_report_lists_a_retained_channel_inside_its_window_as_no_finding(
    environment, tmp_path, capsys
) -> None:
    client = FakeS3()
    _publish_at(tmp_path, "adp", _shared_slack("C1"), client, T0)
    _publish_at(tmp_path, "adp", _shared_slack("C2"), client, T0)
    release_after = T0 + timedelta(hours=72) + TWO_REFRESHES

    code = tenant_config_command.report(_settings(""), "b", client, now=T0)

    out, err = capsys.readouterr()
    assert code == 0, err
    assert f"tenant adp retains slack-channel C1 until {release_after.isoformat()}" in out


def test_the_report_flags_a_retained_channel_past_its_window_and_release_removes_it(
    environment, tmp_path, capsys
) -> None:
    client = FakeS3()
    _publish_at(tmp_path, "adp", _shared_slack("C1"), client, T0)
    _publish_at(tmp_path, "adp", _shared_slack("C2"), client, T0)

    code = tenant_config_command.report(_settings(""), "b", client, now=T0 + timedelta(hours=100))

    assert code == 1
    err = capsys.readouterr().err
    assert "tenant adp retains slack-channel C1 past its release time" in err
    assert "claims-sweep releases it now" in err
    tenant_config_command.release(_settings("adp"), "b", SLACK_CHANNEL, "C1", client)
    assert marker_key(SLACK_CHANNEL, "C1", "adp") not in client.objects
    assert ClaimStore(client, "b").read_index("adp").retained == {}


def test_the_sweep_releases_only_retained_channels_past_their_release_time(
    environment, tmp_path, capsys
) -> None:
    client = FakeS3()
    _publish_at(tmp_path, "adp", _shared_slack("C1"), client, T0)
    _publish_at(tmp_path, "adp", _shared_slack("C2"), client, T0)
    _publish_at(tmp_path, "beta", _shared_slack("C3"), client, T0 + timedelta(hours=90))
    _publish_at(tmp_path, "beta", _shared_slack("C4"), client, T0 + timedelta(hours=90))

    code = tenant_config_command.sweep(_settings(""), "b", client, now=T0 + timedelta(hours=100))

    assert code == 0
    assert marker_key(SLACK_CHANNEL, "C1", "adp") not in client.objects
    assert ClaimStore(client, "b").read_index("adp").retained == {}
    assert marker_key(SLACK_CHANNEL, "C3", "beta") in client.objects
    assert set(ClaimStore(client, "b").read_index("beta").retained) == {Claim(SLACK_CHANNEL, "C3")}
    assert "released slack-channel C1, which tenant adp retained until" in capsys.readouterr().out


def test_the_report_names_the_tenant_holding_a_contested_channel(environment, capsys) -> None:
    client = FakeS3()
    _held(client, "beta", "C1")
    _publish_files(client, "adp", {"escalation.yaml": _shared_slack("C1")})
    _publish_files(client, "beta", {"escalation.yaml": _shared_slack("C1")})

    code = tenant_config_command.report(_settings(""), "b", client)

    assert code == 1
    contested = "tenants adp and beta both claim slack-channel C1"
    assert f"{contested}, and tenant beta holds the earliest marker" in capsys.readouterr().err
