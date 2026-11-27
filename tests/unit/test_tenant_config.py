"""A tenant's config bundle layers over the environment, is validated, and is pinned per run."""

from __future__ import annotations

import io
from collections.abc import Iterator
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
from tests.support import PERMISSIVE_IMAGE_POLICY, configure_tenant, tenant_dir, write_image_policy

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
def environment(tmp_path, monkeypatch) -> Iterator[Path]:
    env = tmp_path / "env"
    env.mkdir()
    (env / "gates.yaml").write_text(GATES)
    (env / "review-trust-root.yaml").write_text("version: 1\n")
    write_image_policy(env)
    configure_tenant(monkeypatch, env)
    tenant_config.reset()
    yield env
    tenant_config.reset()


def _settings(**overrides):
    values = {"tenant": "adp", "ticket_backend": "rebar", "tenant_config_bucket": ""}
    values.update(overrides)
    return get_settings().model_copy(update=values)


def _bundle(tmp_path: Path, files: dict[str, str], *, required: bool = True) -> Path:
    """Write a bundle of the files, over the tenant-only files every valid bundle holds."""
    directory = tmp_path / "bundle"
    directory.mkdir(exist_ok=True)
    defaults = {"repositories.yaml": REPOSITORIES, "tickets.yaml": TICKETS} if required else {}
    for name, text in {**defaults, **files}.items():
        (directory / name).write_text(text)
    return directory


def test_a_tenant_file_comes_only_from_the_bundle_and_a_platform_file_never_does(
    environment, tmp_path
) -> None:
    (environment / "repositories.yaml").write_text(REPOSITORIES.replace("svc", "stray"))
    bundle = _bundle(
        tmp_path,
        {
            "repositories.yaml": REPOSITORIES.replace("svc", "web"),
            "review-trust-root.yaml": "x: 1\n",
        },
    )

    with pinned(bundle):
        assert "web" in load_env_config("repositories")["repositories"]
        assert load_env_config("gates")["profile"] == "private"
        assert env_config_path("review-trust-root") == (environment / "review-trust-root.yaml")

    with pytest.raises(ConfigError, match="config file missing"):
        load_env_config("repositories")


def test_a_replaceable_file_the_bundle_holds_replaces_the_platform_copy(
    environment, tmp_path
) -> None:
    bundle = _bundle(tmp_path, {"gates.yaml": "config_version: 1\nprofile: government\n"})

    with pinned(bundle):
        assert load_env_config("gates")["profile"] == "government"
    assert load_env_config("gates")["profile"] == "private"


def test_a_process_without_a_tenant_reads_no_tenant_file(environment, monkeypatch) -> None:
    monkeypatch.setattr(tenant_config, "tenant_bundle_dir", lambda settings=None: None)

    with pytest.raises(ConfigError, match="reads no tenant's bundle"):
        load_env_config("repositories")


def test_the_platform_directory_may_not_hold_a_tenant_file(environment) -> None:
    (environment / "escalation.yaml").write_text("config_version: 1\n")

    with pytest.raises(ConfigError, match="escalation.yaml, which only a tenant's bundle holds"):
        tenant_config.refuse_platform_tenant_files(environment)


def test_an_unconfigured_process_reads_its_tenant_directory(environment) -> None:
    (tenant_dir(environment) / "repositories.yaml").write_text(REPOSITORIES.replace("svc", "api"))

    assert "api" in load_env_config("repositories")["repositories"]


def test_an_explicit_root_reads_no_bundle(environment, tmp_path) -> None:
    root = tmp_path / "checkout"
    (root / "config" / "local").mkdir(parents=True)
    (root / "config" / "local" / "repositories.yaml").write_text(REPOSITORIES)
    bundle = _bundle(tmp_path, {"repositories.yaml": REPOSITORIES.replace("svc", "web")})

    with pinned(bundle):
        raw = load_env_config("repositories", root=root)

    assert "svc" in raw["repositories"]


def test_a_bundle_of_the_required_files_is_valid(environment, tmp_path) -> None:
    validate_bundle(_bundle(tmp_path, {}), _settings())


def test_an_empty_bundle_is_refused(environment, tmp_path) -> None:
    with pytest.raises(ConfigError, match="repositories.yaml is required"):
        validate_bundle(_bundle(tmp_path, {}, required=False), _settings())


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
        (
            {"integrations.yaml": "config_version: 1\nmodes:\n  pr_closed: sometimes\n"},
            {},
            "integrations.modes.pr_closed must be one of",
        ),
    ],
)
def test_an_unusable_bundle_is_refused(environment, tmp_path, files, settings, reason) -> None:
    with pytest.raises(ConfigError, match=reason):
        validate_bundle(_bundle(tmp_path, files), _settings(**settings))


@pytest.mark.parametrize(
    ("tenant", "reason"),
    [
        ("", "external mode needs credential.username"),
        (
            "config_version: 1\ncredential:\n  username: adp-reader\nmirrors:\n"
            "  quay.io: other.example.com/quay\n",
            "mirror host other.example.com",
        ),
    ],
)
def test_a_refused_tenant_registries_file_refuses_the_bundle(
    environment, tmp_path, tenant, reason
) -> None:
    (environment / "registries.yaml").write_text(
        "config_version: 1\nmode: external\nmirrors:\n  docker.io: art.example.gov/docker\n"
    )
    files = {"registries.yaml": tenant} if tenant else {}

    with pytest.raises(ConfigError, match=reason):
        validate_bundle(_bundle(tmp_path, files), _settings())


def test_a_rebar_tenant_needs_a_tickets_file_in_its_bundle(environment, tmp_path) -> None:
    (environment / "tickets.yaml").write_text(TICKETS)

    with pytest.raises(ConfigError, match="tickets.yaml is required with TICKET_BACKEND=rebar"):
        validate_bundle(
            _bundle(tmp_path, {"repositories.yaml": REPOSITORIES}, required=False), _settings()
        )


def _tenant_exceptions(image: str) -> str:
    from datetime import UTC, datetime, timedelta

    today = datetime.now(UTC).date()
    return (
        "config_version: 1\n"
        "exceptions:\n"
        f"  - image: {image}@sha256:{'a' * 64}\n"
        "    finding: CVE-1\n"
        "    reason: Vendor fix pending.\n"
        f"    created: {today.isoformat()}\n"
        f"    expires: {(today + timedelta(days=7)).isoformat()}\n"
    )


def test_a_bundle_exceptions_file_is_held_only_within_the_tenants_scope(
    environment, tmp_path, caplog
) -> None:
    write_image_policy(
        environment,
        PERMISSIVE_IMAGE_POLICY + "platform_registries: [ecr.example]\n"
        "tenant_prefix: ecr.example/tenants/{tenant}/\n",
    )

    bundle = _bundle(
        tmp_path, {"image_exceptions.yaml": _tenant_exceptions("ecr.example/tenants/adp/app")}
    )

    with caplog.at_level("WARNING"):
        validate_bundle(bundle, _settings())
    assert f"{environment / 'image_exceptions.yaml'} is missing" in caplog.text
    with pinned(bundle):
        assert env_config_path("image_exceptions") == (environment / "image_exceptions.yaml")
    with pytest.raises(ConfigError, match=r"image_exceptions.yaml exceptions\[0\] accepts CVE-1"):
        validate_bundle(
            _bundle(
                tmp_path, {"image_exceptions.yaml": _tenant_exceptions("ecr.example/platform/x")}
            ),
            _settings(),
        )


def test_a_new_valid_bundle_activates_and_an_invalid_one_keeps_the_last(
    environment, tmp_path
) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "repositories.yaml").write_text(REPOSITORIES)
    (source_dir / "tickets.yaml").write_text(TICKETS)
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
    assert config.active is not None and config.active_path is not None
    assert config.active.files.keys() == {"repositories", "tickets"}
    assert "web:" in (config.active_path / "repositories.yaml").read_text()


def test_an_empty_source_refuses_to_start_and_names_its_location(environment, tmp_path) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()

    with pytest.raises(ConfigError, match=f"{source_dir} holds no published bundle of tenant adp"):
        tenant_config.configure(
            _settings(), source=DirectorySource(source_dir, "adp"), store_root=tmp_path / "store"
        )


def test_a_first_bundle_that_fails_validation_refuses_to_start(environment, tmp_path) -> None:
    source_dir = _bundle(tmp_path, {"repositories.yaml": "config_version: 1\nrepositories: {}\n"})

    with pytest.raises(ConfigError, match="names no repository"):
        tenant_config.configure(
            _settings(), source=DirectorySource(source_dir, "adp"), store_root=tmp_path / "store"
        )


def test_a_pinned_bundle_is_read_from_the_store_and_another_tenants_is_refused(
    environment, tmp_path
) -> None:
    source_dir = _bundle(tmp_path, {})
    config = tenant_config.configure(
        _settings(), source=DirectorySource(source_dir, "adp"), store_root=tmp_path / "store"
    )
    pinned_identity = config.active
    assert pinned_identity is not None

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
    from tests.unit.test_tenant_claims_store import FakeS3

    client = FakeS3()
    client.put_object(Bucket="b", Key="tenants/adp/checks.yaml", Body=b"old")
    bundle = _bundle(tmp_path, {"repositories.yaml": REPOSITORIES, "gates.yaml": GATES})

    versions = publish(bundle, _settings(), "b", client=client)

    assert versions == {"repositories": "v1", "tickets": "v1", "gates": "v1"}
    assert "tenants/adp/checks.yaml" not in client.objects


def test_a_worker_without_a_tenant_refuses_to_start() -> None:
    from services.orchestration.tenant_pinning import start_tenant_config

    with pytest.raises(ConfigError, match="TENANT is not set"):
        start_tenant_config(_settings(tenant=""))


def test_validation_settings_apply_the_tenant_file_over_the_base_settings(
    environment, monkeypatch
) -> None:
    import scripts.config
    from scripts import tenant_config as tenant_config_command
    from tests.support import hermetic_settings

    base = hermetic_settings(
        tenant="adp", ticket_backend="jira", temporal_namespace="platform", log_level="INFO"
    )
    monkeypatch.setattr(scripts.config, "get_settings", lambda: base)
    (environment / "tenants" / "beta.env").write_text(
        "TICKET_BACKEND=rebar\nLOG_LEVEL=debug\nNOT_A_SETTING=x\n"
    )

    beta = tenant_config_command._settings("beta")
    adp = tenant_config_command._settings("")

    assert (beta.tenant, beta.ticket_backend, beta.log_level) == ("beta", "rebar", "DEBUG")
    assert beta.temporal_namespace == "platform"
    assert (adp.tenant, adp.ticket_backend, adp.log_level) == ("adp", "jira", "INFO")
    assert base.ticket_backend == "jira"


def test_a_tenant_settings_value_is_validated_as_its_field() -> None:
    from pydantic import ValidationError
    from scripts.tenant_config import apply_tenant_settings
    from tests.support import hermetic_settings

    with pytest.raises(ValidationError, match="LOG_LEVEL"):
        apply_tenant_settings(hermetic_settings(), {"LOG_LEVEL": "loud"})


def test_tenant_source_reads_the_named_tenants_directory(monkeypatch, tmp_path) -> None:
    from tests.support import hermetic_settings

    configure_tenant(monkeypatch, tmp_path)
    source = tenant_config.tenant_source(hermetic_settings(tenant="adp"), "beta")

    assert isinstance(source, tenant_config.DirectorySource)
    assert (source.path, source.tenant) == (tmp_path / "tenants" / "beta", "beta")


def test_tenant_source_reads_the_named_tenants_bucket_prefix(monkeypatch) -> None:
    import scripts.aws_clients
    from tests.support import hermetic_settings

    client = object()
    monkeypatch.setattr(scripts.aws_clients, "configure_aws_fips", lambda settings: None)
    monkeypatch.setattr(scripts.aws_clients, "s3_client", lambda **kwargs: client)
    settings = hermetic_settings(tenant="adp", tenant_config_bucket="config-bucket")

    source = tenant_config.tenant_source(settings, "beta")

    assert isinstance(source, tenant_config.S3Source)
    assert (source.client, source.bucket) == (client, "config-bucket")
    assert source.prefix == "tenants/beta/"


def test_the_terraform_platform_config_refuses_exactly_the_tenant_only_files() -> None:
    import re

    variables = Path(__file__).resolve().parents[2] / "infra/terraform/platform-config/variables.tf"
    text = variables.read_text(encoding="utf-8")
    condition = re.search(r"setintersection\(.*?\[(.*?)\]", text, re.DOTALL)
    message = re.search(r"Each of (.*?) belongs in the tenant", text)
    expected = {f"{name}.yaml" for name in tenant_config.TENANT_ONLY_FILES}

    assert condition is not None and message is not None
    assert set(re.findall(r'"([a-z_]+\.yaml)"', condition.group(1))) == expected
    assert set(re.findall(r"[a-z_]+\.yaml", message.group(1))) == expected


def test_the_tenant_template_validates_as_a_bundle_with_every_optional_file(
    environment, tmp_path
) -> None:
    local = Path(__file__).resolve().parents[2] / "config/local"
    template = local / "tenants/tenant.example"
    isolation = (local / "tickets_isolation.yaml.example").read_text()
    (environment / "tickets_isolation.yaml").write_text(isolation)
    directory = tmp_path / "bundle"
    directory.mkdir()
    for path in template.iterdir():
        if path.name != "registries.yaml.example":
            text = path.read_text().replace("<tenant>", "adp")
            (directory / path.name.removesuffix(".example")).write_text(text)
    runtime = ("agent-server-python-node", "checks-python")
    engines = ("security-policy", "test-policy")
    images = {name: f"registry.example/{name}:1@sha256:" + "e" * 64 for name in runtime + engines}
    settings = _settings(
        runtime_images={name: images[name] for name in runtime},
        gate_engine_images={name: images[name] for name in engines},
    )

    validate_bundle(directory, settings)


def test_an_operator_command_on_a_bucket_reads_tenant_files_from_the_latest_bundle(
    environment, tmp_path, monkeypatch
) -> None:
    from scripts.tenant_bundle import activate_tenant_bundle

    settings = _settings(tenant_config_bucket="tenant-config")
    monkeypatch.setattr("scripts.config.get_settings", lambda: settings)
    source_dir = _bundle(tmp_path, {"repositories.yaml": REPOSITORIES.replace("svc", "web")})
    monkeypatch.setattr(
        tenant_config, "build_source", lambda _settings: DirectorySource(source_dir, "adp")
    )
    monkeypatch.setattr(tenant_config, "_default_store_root", lambda: tmp_path / "store")
    with pytest.raises(ConfigError, match="reads no tenant's bundle"):
        load_env_config("repositories")

    activate_tenant_bundle(settings)

    assert "web" in load_env_config("repositories")["repositories"]


def test_an_operator_command_without_a_bucket_keeps_reading_its_tenant_directory(
    environment,
) -> None:
    from scripts.tenant_bundle import activate_tenant_bundle

    (tenant_dir(environment) / "repositories.yaml").write_text(REPOSITORIES.replace("svc", "api"))

    activate_tenant_bundle(_settings())

    assert tenant_config.active_tenant_config() is None
    assert "api" in load_env_config("repositories")["repositories"]


def test_a_restart_in_the_worker_container_reads_the_rebar_tickets_config(
    environment, tmp_path, monkeypatch
) -> None:
    from types import SimpleNamespace

    from scripts import restart
    from services.integration.tickets.rebar_config import load_rebar_backend_config

    settings = _settings(tenant_config_bucket="tenant-config")
    monkeypatch.setattr("scripts.config.get_settings", lambda: settings)
    source_dir = _bundle(tmp_path, {})
    monkeypatch.setattr(
        tenant_config, "build_source", lambda _settings: DirectorySource(source_dir, "adp")
    )
    monkeypatch.setattr(tenant_config, "_default_store_root", lambda: tmp_path / "store")
    roots = []

    async def run_restart_task(issue_key, *, actor, repo):
        roots.append(load_rebar_backend_config().repo_root)
        return SimpleNamespace(restarted=True, message=f"{issue_key} restarted")

    monkeypatch.setattr("services.orchestration.task_restart.run_restart_task", run_restart_task)
    monkeypatch.setattr("sys.argv", ["restart", "T-1"])

    assert restart.main() == 0
    assert roots == ["/srv/rebar-tickets"]
