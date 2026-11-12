"""A tenant's config bundle layers over the environment, is validated, and is pinned per run."""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts.config import get_settings
from services.integration import tenant_config
from services.integration.credentials import ConfigError, env_config_path, load_env_config
from services.integration.tenant_config import (
    BundleIdentity,
    BundleStore,
    DirectorySource,
    S3Source,
    TenantConfig,
    current_identity,
    pinned,
    validate_bundle,
)
from tests.support import write_image_policy

REPOSITORIES = """config_version: 1
defaults:
  backend: local
repositories:
  svc:
    path: ../svc
"""
GATES = """config_version: 1
profile: private
gates:
  - name: semgrep
    enabled: false
"""
TICKETS = """config_version: 1
backend:
  repo_root: /srv/rebar-tickets
  project: ADSER
  default_repo: svc
  status_flow:
    work_started: in_progress
    ready_for_review: in_progress
    terminal: closed
"""


@pytest.fixture
def environment(tmp_path, monkeypatch) -> Path:
    env = tmp_path / "env"
    env.mkdir()
    (env / "repositories.yaml").write_text(REPOSITORIES)
    (env / "gates.yaml").write_text(GATES)
    (env / "tickets.yaml").write_text(TICKETS)
    (env / "review-trust-root.yaml").write_text("version: 1\n")
    write_image_policy(env)
    monkeypatch.setenv("PLATFORM_ENV_CONFIG_DIR", str(env))
    tenant_config.reset()
    yield env
    tenant_config.reset()


def _settings(**overrides):
    values = {"tenant": "adp", "ticket_backend": "rebar", "tenant_config_bucket": ""}
    values.update(overrides)
    return get_settings().model_copy(update=values)


def _bundle(tmp_path: Path, files: dict[str, str]) -> Path:
    directory = tmp_path / "bundle"
    directory.mkdir(exist_ok=True)
    for name, text in files.items():
        (directory / name).write_text(text)
    return directory


def test_a_bundle_file_overrides_the_environment_and_a_platform_file_never_does(
    environment, tmp_path
) -> None:
    bundle = _bundle(
        tmp_path,
        {
            "repositories.yaml": REPOSITORIES.replace("svc", "web"),
            "review-trust-root.yaml": "x: 1\n",
        },
    )

    with pinned(bundle):
        assert "web" in load_env_config("repositories", environment="test")["repositories"]
        assert load_env_config("gates", environment="test")["profile"] == "private"
        assert env_config_path("review-trust-root", environment="test") == (
            environment / "review-trust-root.yaml"
        )

    assert "svc" in load_env_config("repositories", environment="test")["repositories"]


def test_an_unconfigured_process_reads_its_tenant_directory(environment, monkeypatch) -> None:
    tenant_dir = environment / "tenants" / "adp"
    tenant_dir.mkdir(parents=True)
    (tenant_dir / "repositories.yaml").write_text(REPOSITORIES.replace("svc", "api"))
    monkeypatch.setattr(tenant_config, "tenant_bundle_dir", lambda settings=None: tenant_dir)

    assert "api" in load_env_config("repositories", environment="test")["repositories"]


def test_an_explicit_root_reads_no_bundle(environment, tmp_path) -> None:
    root = tmp_path / "checkout"
    (root / "config" / "test").mkdir(parents=True)
    (root / "config" / "test" / "repositories.yaml").write_text(REPOSITORIES)
    bundle = _bundle(tmp_path, {"repositories.yaml": REPOSITORIES.replace("svc", "web")})

    with pinned(bundle):
        raw = load_env_config("repositories", environment="test", root=root)

    assert "svc" in raw["repositories"]


def test_the_environment_alone_is_a_valid_bundle(environment, tmp_path) -> None:
    validate_bundle(_bundle(tmp_path, {}), _settings())


@pytest.mark.parametrize(
    ("files", "settings", "reason"),
    [
        (
            {"review-trust-root.yaml": "x: 1\n"},
            {},
            "review-trust-root.yaml is not a tenant config file",
        ),
        ({"repositories.yaml": "config_version: 1\nrepositories: {}\n"}, {}, "names no repository"),
        ({}, {"ticket_backend": "jira"}, "jira.yaml is required with TICKET_BACKEND=jira"),
        (
            {"repositories.yaml": REPOSITORIES.replace("backend: local", "backend: github")},
            {},
            "github.yaml is required",
        ),
        ({"gates.yaml": "config_version: 1\ngates: [{name: no-such-gate}]\n"}, {}, "gates.yaml"),
    ],
)
def test_an_unusable_bundle_is_refused(environment, tmp_path, files, settings, reason) -> None:
    with pytest.raises(ConfigError, match=reason):
        validate_bundle(_bundle(tmp_path, files), _settings(**settings))


def test_a_rebar_tenant_needs_a_tickets_file_from_the_bundle_or_the_environment(
    environment, tmp_path
) -> None:
    (environment / "tickets.yaml").unlink()

    with pytest.raises(ConfigError, match="tickets.yaml is required with TICKET_BACKEND=rebar"):
        validate_bundle(_bundle(tmp_path, {}), _settings())
    validate_bundle(_bundle(tmp_path, {"tickets.yaml": TICKETS}), _settings())


def test_a_new_valid_bundle_activates_and_an_invalid_one_keeps_the_last(
    environment, tmp_path
) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    rejected = []
    config = TenantConfig(
        _settings(),
        DirectorySource(source_dir, "adp"),
        BundleStore(tmp_path / "store", "adp"),
        on_rejected=lambda identity, reason: rejected.append(identity.id),
    )

    assert config.refresh() is True
    first = config.active
    assert config.refresh() is False

    (source_dir / "repositories.yaml").write_text("config_version: 1\nrepositories: {}\n")
    assert config.refresh() is False
    assert config.refresh() is False
    assert config.active == first
    assert len(rejected) == 1

    (source_dir / "repositories.yaml").write_text(REPOSITORIES.replace("svc", "web"))
    assert config.refresh() is True
    assert config.active.files.keys() == {"repositories"}
    assert "web:" in (config.active_path / "repositories.yaml").read_text()


def test_a_first_bundle_that_fails_validation_refuses_to_start(environment, tmp_path) -> None:
    source_dir = _bundle(tmp_path, {"repositories.yaml": "config_version: 1\nrepositories: {}\n"})

    with pytest.raises(ConfigError, match="names no repository"):
        tenant_config.configure(
            _settings(), source=DirectorySource(source_dir, "adp"), store_root=tmp_path / "store"
        )


def test_a_pinned_bundle_is_read_from_the_store_and_another_tenants_is_refused(
    environment, tmp_path
) -> None:
    source_dir = _bundle(tmp_path, {"repositories.yaml": REPOSITORIES})
    config = tenant_config.configure(
        _settings(), source=DirectorySource(source_dir, "adp"), store_root=tmp_path / "store"
    )
    pinned_identity = config.active

    (source_dir / "repositories.yaml").write_text(REPOSITORIES.replace("svc", "web"))
    config.refresh()

    path = config.path_for(pinned_identity)
    assert "svc" in (path / "repositories.yaml").read_text()
    with pytest.raises(ConfigError, match="tenant payments"):
        config.path_for(BundleIdentity("payments", "directory", {}))
    with pinned(path, pinned_identity):
        assert current_identity() == pinned_identity
    assert current_identity() == config.active


class _FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, list[tuple[str, bytes]]] = {}
        self.deleted: list[str] = []

    def put_object(self, *, Bucket, Key, Body):
        versions = self.objects.setdefault(Key, [])
        versions.append((f"v{len(versions) + 1}", Body))

    def delete_object(self, *, Bucket, Key):
        self.deleted.append(Key)
        self.objects.pop(Key, None)

    def get_paginator(self, name):
        assert name == "list_object_versions"
        return SimpleNamespace(paginate=self._pages)

    def _pages(self, *, Bucket, Prefix):
        versions = []
        for key, history in self.objects.items():
            if key.startswith(Prefix):
                for index, (version, _) in enumerate(history):
                    versions.append(
                        {"Key": key, "VersionId": version, "IsLatest": index == len(history) - 1}
                    )
        return [{"Versions": versions}]

    def get_object(self, *, Bucket, Key, VersionId):
        body = dict(self.objects[Key])[VersionId]
        return {"Body": io.BytesIO(body)}


def test_the_bucket_source_lists_the_latest_versions_and_fetches_any_version() -> None:
    client = _FakeS3()
    client.put_object(Bucket="b", Key="tenants/adp/gates.yaml", Body=b"one")
    client.put_object(Bucket="b", Key="tenants/adp/gates.yaml", Body=b"two")
    client.put_object(Bucket="b", Key="tenants/adp/profiles/node.yaml", Body=b"nested")
    client.put_object(Bucket="b", Key="tenants/payments/gates.yaml", Body=b"other")
    source = S3Source(client, "b", "adp")

    latest = source.latest()

    assert latest.files == {"gates": "v2"}
    assert source.fetch(latest).files == {"gates": "two"}
    assert source.fetch(BundleIdentity("adp", "s3", {"gates": "v1"})).files == {"gates": "one"}


def test_publish_uploads_the_bundle_and_deletes_what_it_dropped(environment, tmp_path) -> None:
    from scripts.tenant_config import publish

    client = _FakeS3()
    client.put_object(Bucket="b", Key="tenants/adp/checks.yaml", Body=b"old")
    bundle = _bundle(tmp_path, {"repositories.yaml": REPOSITORIES, "gates.yaml": GATES})

    versions = publish(bundle, _settings(), "b", client=client)

    assert versions == {"repositories": "v1", "gates": "v1"}
    assert client.deleted == ["tenants/adp/checks.yaml"]


def test_a_worker_without_a_tenant_refuses_to_start() -> None:
    from services.orchestration.tenant_pinning import start_tenant_config

    with pytest.raises(ConfigError, match="TENANT is not set"):
        start_tenant_config(_settings(tenant=""))
