"""A repository's runtime profile selects its sandbox image, default checks, and gate overrides."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from scripts.config import get_settings
from services.integration import tenant_config
from services.integration.credentials import ConfigError
from services.integration.runtime_profiles import (
    bound_repository,
    catalog_profiles,
    current_repository,
    load_profiles,
    resolve_image,
    sandbox_image,
)
from services.integration.tenant_config import validate_bundle
from tests.support import write_image_policy

ENGINE_IMAGES = {
    "security-policy": "registry.example/gate-engine-security-policy:1@sha256:" + "a" * 64,
    "test-policy": "registry.example/gate-engine-test-policy:1@sha256:" + "b" * 64,
}
JVM = "registry.example/agent-server-jvm:1@sha256:" + "c" * 64
REPOSITORIES = """config_version: 1
defaults:
  backend: github
repositories:
  api:
    profile: python-fastapi
  portal:
    profile: node-react
  claims:
    profile: java-spring
  plain: {}
"""
GITHUB = """config_version: 2
hosts:
  github-com:
    api_url: https://api.github.com
    git_base_url: https://github.com
default:
  host: github-com
  owner: example-org
  token_ref: GITHUB_TOKEN
"""
GATES = """config_version: 1
profile: private
gates:
  - name: semgrep
    enabled: false
  - name: trivy
    enabled: false
"""
TICKETS = """config_version: 1
backend:
  repo_root: /srv/rebar-tickets
  project: ADSER
  default_repo: api
  status_flow:
    work_started: in_progress
    ready_for_review: in_progress
    terminal: closed
"""


@pytest.fixture
def environment(tmp_path, monkeypatch):
    env = tmp_path / "env"
    env.mkdir()
    for name, text in {
        "repositories.yaml": REPOSITORIES,
        "github.yaml": GITHUB,
        "gates.yaml": GATES,
        "tickets.yaml": TICKETS,
    }.items():
        (env / name).write_text(text)
    write_image_policy(env)
    monkeypatch.setenv("PLATFORM_ENV_CONFIG_DIR", str(env))
    tenant_config.reset()
    yield env
    tenant_config.reset()


def _settings(**overrides):
    values = {
        "tenant": "adp",
        "ticket_backend": "rebar",
        "tenant_config_bucket": "",
        "gate_engine_images": ENGINE_IMAGES,
        "runtime_images": {"agent-server-jvm": JVM},
    }
    values.update(overrides)
    return get_settings().model_copy(update=values)


def test_the_catalog_offers_a_profile_per_stack_with_pinned_checks_images() -> None:
    profiles = catalog_profiles()

    assert set(profiles) == {"python-fastapi", "node-react", "java-spring", "go-service"}
    assert profiles["node-react"].sandbox_image == "catalog:agent-server-python-node"
    assert profiles["java-spring"].sandbox_image == "catalog:agent-server-jvm"
    assert all("@sha256:" in profile.checks["image"] for profile in profiles.values())


def test_catalog_images_resolve_and_another_reference_passes_through() -> None:
    settings = _settings()

    assert resolve_image("catalog:agent-server-python-node", settings) == (
        settings.openhands_agent_server_image
    )
    assert resolve_image("catalog:agent-server-jvm", settings) == JVM
    assert resolve_image("registry.example/custom@sha256:" + "d" * 64, settings).endswith("d" * 64)
    with pytest.raises(ConfigError, match="agent-server-go is not published"):
        resolve_image("catalog:agent-server-go", settings)


def test_a_repository_runs_in_its_profiles_sandbox_image(environment) -> None:
    settings = _settings()

    assert sandbox_image("claims", settings) == JVM
    assert sandbox_image("portal", settings) == settings.openhands_agent_server_image
    assert sandbox_image("plain", settings) == settings.openhands_agent_server_image


def test_a_profile_supplies_checks_unless_the_repository_has_its_own(environment) -> None:
    from services.orchestration.checks.config import checks_for_repo, required_checks_repos

    settings = _settings()
    (environment / "checks.yaml").write_text(
        "config_version: 1\nchecks:\n  api:\n    image: python:3.12-slim\n    command: make test\n"
    )

    assert checks_for_repo("api", settings).command == "make test"
    assert checks_for_repo("claims", settings).command == "mvn -B -q verify"
    assert checks_for_repo("plain", settings) is None
    assert required_checks_repos(settings) == frozenset({"api", "portal", "claims"})


def test_the_bound_repositorys_profile_overrides_the_tenant_gates(environment) -> None:
    from services.orchestration.gates.config import load_gates_config

    settings = _settings()

    with bound_repository("portal"):
        assert current_repository() == "portal"
        enabled = {spec.name for spec in load_gates_config(settings).enabled_gates()}
    plain = {spec.name for spec in load_gates_config(settings).enabled_gates()}

    assert {"security-policy", "test-policy"} <= enabled
    assert not {"security-policy", "test-policy"} & plain


def test_a_tenant_profile_may_not_reuse_a_catalog_name(environment) -> None:
    (environment / "profiles.yaml").write_text(
        "config_version: 1\nprofiles:\n  node-react:\n    sandbox_image: x\n"
        "  claims-batch:\n    sandbox_image: catalog:agent-server-jvm\n"
    )

    with pytest.raises(ConfigError, match="reuses the catalog profile names node-react"):
        load_profiles(_settings())


def test_a_bundle_whose_repository_cannot_run_its_profile_is_refused(environment, tmp_path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()

    validate_bundle(bundle, _settings())
    with pytest.raises(ConfigError, match="agent-server-jvm is not published"):
        validate_bundle(bundle, _settings(runtime_images={}))
    with pytest.raises(ConfigError, match="'security-policy' is enabled but not registered"):
        validate_bundle(bundle, _settings(gate_engine_images={}))
    (bundle / "repositories.yaml").write_text(REPOSITORIES.replace("java-spring", "cobol"))
    with pytest.raises(ConfigError, match="names the profile 'cobol'"):
        validate_bundle(bundle, _settings())


def test_a_tenant_engine_may_not_reuse_a_catalog_engine_name(environment) -> None:
    from services.orchestration.gates.engine_source import load_gate_engine_configs

    (environment / "gate_engine.yaml").write_text(
        "config_version: 1\nengines:\n  - name: security-policy\n    enabled: true\n"
        "    mode: image\n    review_entrypoint: bin/scan\n"
    )

    with pytest.raises(ConfigError, match="redefines the catalog engines security-policy"):
        load_gate_engine_configs(_settings())


def test_catalog_engines_register_only_where_their_image_is_published(environment) -> None:
    from services.orchestration.gates.engine_source import load_gate_engine_configs

    published = load_gate_engine_configs(_settings(gate_engine_images={"test-policy": "x"}))

    assert [(engine.name, engine.image) for engine in published] == [("test-policy", "x")]


async def test_each_activity_binds_the_repository_its_input_names(environment, tmp_path) -> None:
    from services.integration.tenant_config import DirectorySource
    from services.orchestration.tenant_pinning import TenantConfigInterceptor

    config = tenant_config.configure(
        _settings(), source=DirectorySource(tmp_path / "none", "adp"), store_root=tmp_path / "s"
    )
    seen = []

    class _Next:
        async def execute_activity(self, input):
            seen.append(current_repository())

    async def lookup(execution_id):
        return None

    inbound = TenantConfigInterceptor(config, lookup).intercept_activity(_Next())
    context = SimpleNamespace(execution_id="exec-1", repo="claims")

    await inbound.execute_activity(SimpleNamespace(args=[SimpleNamespace(context=context)]))
    await inbound.execute_activity(SimpleNamespace(args=[{"execution_id": "e", "repo": "api"}]))

    assert seen == ["claims", "api"]
    assert current_repository() == ""
