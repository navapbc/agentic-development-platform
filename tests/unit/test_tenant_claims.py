"""A tenant's config claims the values only one tenant may hold, and a clash is refused."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import scripts.config
from scripts import tenant_config as tenant_config_command
from services.integration import tenant_config
from services.integration.tenant_claims import (
    JIRA_PROJECT,
    REGISTRY_USERNAME,
    SLACK_APP,
    SLACK_CHANNEL,
    SLACK_OWN_CHANNEL,
    Claim,
    bundle_claims,
    claim_markers,
    find_clashes,
)
from tests.support import SHARED_APP_YAML, hermetic_settings, write_image_policy
from tests.unit.test_tenant_config import GATES, REPOSITORIES, TICKETS

SHARED_APP = SHARED_APP_YAML


def _shared_slack(channel: str) -> str:
    return f"""config_version: 1
channels: [jira, slack]
slack:
  app: shared
  transport: webhook
  channel: {channel}
  profile: private
"""


def _own_slack(channel: str, app_id: str) -> str:
    return f"""config_version: 1
channels: [slack]
slack:
  transport: socket
  channel: {channel}
  organization_id: T0000000001
  app_id: {app_id}
  profile: private
"""


def _jira(projects: str, label: str = "adp", sites: str = "") -> str:
    return f"""config_version: 1
allowlist: [OPS, PLAT]
sites:
  cloud:
    url: https://Acme.atlassian.net:443/jira
    auth_mode: cloud
{sites}
default:
  site: cloud
  email: agent@example.com
  token_ref: JIRA_API_TOKEN
  board: OPS
  intake_label: {label}
projects:
{projects}
"""


def _directory(path: Path, files: dict[str, str]) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (path / name).write_text(text)
    return path


def _claims(tmp_path: Path, bundle: dict[str, str], backend: str = "rebar"):
    files = tenant_config_command.tenant_files(_directory(tmp_path / "bundle", bundle))
    return bundle_claims(files, backend)


def test_a_shared_app_tenant_claims_its_channel(tmp_path: Path) -> None:
    claims = _claims(tmp_path, {"escalation.yaml": _shared_slack("C1")})

    assert claims == {Claim(SLACK_CHANNEL, "C1")}


def test_a_tenant_on_its_own_app_claims_the_app_and_its_channel_uppercased(
    tmp_path: Path,
) -> None:
    claims = _claims(tmp_path, {"escalation.yaml": _own_slack("c0abc", "a0abc")})

    assert claims == {Claim(SLACK_APP, "A0ABC"), Claim(SLACK_OWN_CHANNEL, "C0ABC")}


def test_a_bundle_without_escalation_claims_no_channel(tmp_path: Path) -> None:
    assert _claims(tmp_path, {}) == frozenset()


def test_a_jira_tenant_claims_each_project_on_its_canonical_site_with_its_label(
    tmp_path: Path,
) -> None:
    jira = _jira("  PLAT:\n    intake_label: plat-adp")

    claims = _claims(tmp_path, {"jira.yaml": jira}, backend="jira")

    assert claims == {
        Claim(JIRA_PROJECT, "https://acme.atlassian.net/OPS/adp"),
        Claim(JIRA_PROJECT, "https://acme.atlassian.net/PLAT/plat-adp"),
    }


def test_a_rebar_tenant_claims_no_jira_project(tmp_path: Path) -> None:
    assert _claims(tmp_path, {"jira.yaml": _jira("  {}")}, backend="rebar") == frozenset()


def test_one_project_under_two_intake_labels_or_on_two_sites_does_not_clash(
    tmp_path: Path,
) -> None:
    other_site = "  other:\n    url: https://bücher.example\n    auth_mode: datacenter\n"
    bundles = {
        "adp": _jira("  {}", label="adp"),
        "beta": _jira("  {}", label="beta"),
        "gamma": _jira(
            "  OPS:\n    site: other\n  PLAT:\n    site: other", label="adp", sites=other_site
        ),
    }
    claims = {tenant: bundle_claims({"jira": text}, "jira") for tenant, text in bundles.items()}

    assert Claim(JIRA_PROJECT, "https://acme.atlassian.net/OPS/beta") in claims["beta"]
    assert Claim(JIRA_PROJECT, "https://xn--bcher-kva.example/OPS/adp") in claims["gamma"]
    assert find_clashes(claims) == []


def test_two_tenants_on_one_jira_project_and_label_clash(tmp_path: Path) -> None:
    claims = {
        tenant: bundle_claims(
            tenant_config_command.tenant_files(
                _directory(tmp_path / tenant, {"jira.yaml": _jira("  {}")})
            ),
            "jira",
        )
        for tenant in ("adp", "beta")
    }

    [clash] = [c for c in find_clashes(claims) if "/OPS/" in str(c)]

    assert str(clash) == (
        "tenants adp and beta both claim jira-project https://acme.atlassian.net/OPS/adp"
    )


def test_tenants_on_their_own_apps_share_a_channel_and_a_shared_app_tenant_may_not() -> None:
    own = {
        "adp": {Claim(SLACK_APP, "A1"), Claim(SLACK_OWN_CHANNEL, "C1")},
        "beta": {Claim(SLACK_APP, "A2"), Claim(SLACK_OWN_CHANNEL, "C1")},
    }

    assert find_clashes(own) == []
    clashes = find_clashes({**own, "gamma": {Claim(SLACK_CHANNEL, "C1")}})
    assert [str(clash) for clash in clashes] == [
        "tenant adp claims slack-own-channel C1 and tenant gamma claims slack-channel C1",
        "tenant beta claims slack-own-channel C1 and tenant gamma claims slack-channel C1",
    ]


def test_two_tenants_on_one_shared_channel_or_one_app_clash() -> None:
    clashes = find_clashes(
        {
            "adp": {Claim(SLACK_CHANNEL, "C1")},
            "beta": {Claim(SLACK_CHANNEL, "C1")},
            "gamma": {Claim(SLACK_APP, "A1")},
            "delta": {Claim(SLACK_APP, "A1")},
        }
    )

    assert sorted(str(clash) for clash in clashes) == [
        "tenants adp and beta both claim slack-channel C1",
        "tenants delta and gamma both claim slack-app A1",
    ]


class _FakeS3:
    def __init__(self, objects: dict[str, datetime]) -> None:
        self.objects = objects
        self.prefixes: list[str] = []

    def get_paginator(self, operation: str):
        assert operation == "list_objects_v2"
        return self

    def paginate(self, *, Bucket: str, Prefix: str):
        self.prefixes.append(Prefix)
        contents = [
            {"Key": key, "LastModified": modified}
            for key, modified in self.objects.items()
            if key.startswith(Prefix)
        ]
        return [{"Contents": contents[:2]}, {"Contents": contents[2:]}]


def test_the_markers_of_one_claim_are_read_from_its_digest_prefix_earliest_first() -> None:
    digest = hashlib.sha256(b"slack-channel\0C1").hexdigest()
    other = hashlib.sha256(b"slack-own-channel\0C1").hexdigest()
    prefix = f"claims/slack-channel/{digest}/"
    client = _FakeS3(
        {
            f"{prefix}beta": datetime(2026, 11, 2, tzinfo=UTC),
            f"{prefix}nested/gamma": datetime(2026, 10, 1, tzinfo=UTC),
            f"{prefix}": datetime(2026, 10, 1, tzinfo=UTC),
            f"{prefix}adp": datetime(2026, 11, 1, tzinfo=UTC),
            f"claims/slack-own-channel/{other}/delta": datetime(2026, 9, 1, tzinfo=UTC),
        }
    )

    markers = claim_markers(client, "config-bucket", SLACK_CHANNEL, "C1")

    assert client.prefixes == [prefix]
    assert [(m.tenant, m.last_modified) for m in markers] == [
        ("adp", datetime(2026, 11, 1, tzinfo=UTC)),
        ("beta", datetime(2026, 11, 2, tzinfo=UTC)),
    ]


def _tenant_files(channel: str) -> dict[str, str]:
    return {
        "repositories.yaml": REPOSITORIES,
        "tickets.yaml": TICKETS,
        "escalation.yaml": _shared_slack(channel),
    }


@pytest.fixture
def environment(tmp_path, monkeypatch) -> Iterator[Path]:
    env = _directory(tmp_path / "env", {"gates.yaml": GATES, "slack-shared-app.yaml": SHARED_APP})
    write_image_policy(env)
    _directory(env / "tenants" / "adp", _tenant_files("C1"))
    _directory(env / "tenants" / "gamma", {"escalation.yaml": _shared_slack("C2")})
    _directory(env / "tenants" / "tenant.example", {"escalation.yaml": _shared_slack("C3")})
    monkeypatch.setenv("PLATFORM_ENV_CONFIG_DIR", str(env))
    base = hermetic_settings(tenant="adp", ticket_backend="rebar")
    monkeypatch.setattr(scripts.config, "get_settings", lambda: base)
    tenant_config.reset()
    yield env
    tenant_config.reset()


@pytest.mark.parametrize(
    ("channel", "refusal"),
    [
        ("C1", "tenants adp and beta both claim slack-channel C1"),
        ("C2", "tenants beta and gamma both claim slack-channel C2"),
        ("C3", None),
    ],
)
def test_validate_refuses_a_value_another_local_tenant_holds(
    environment, tmp_path, capsys, channel: str, refusal: str | None
) -> None:
    bundle = _directory(tmp_path / "bundle", _tenant_files(channel))
    settings = hermetic_settings(tenant="beta", ticket_backend="rebar")

    valid = tenant_config_command.validate(bundle, settings)

    assert valid is (refusal is None)
    assert (refusal or "") in capsys.readouterr().err


def test_two_tenants_with_one_registry_username_clash_in_any_case() -> None:
    registries = {
        tenant: f"config_version: 1\ncredential:\n  username: {username}\n"
        for tenant, username in (("adp", "shared-reader"), ("beta", "Shared-Reader"))
    }
    claims = {
        tenant: bundle_claims({"registries": text}, "rebar") for tenant, text in registries.items()
    }

    assert Claim(REGISTRY_USERNAME, "shared-reader") in claims["adp"]
    assert [str(clash) for clash in find_clashes(claims)] == [
        "tenants adp and beta both claim registry-username shared-reader"
    ]
