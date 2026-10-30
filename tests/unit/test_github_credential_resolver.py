"""Unit tests for the GitHub credential resolver: named hosts, default identity, overrides."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from services.integration.credentials import (
    ConfigError,
    EnvSecretResolver,
    SecretNotFoundError,
    load_env_config,
    load_platform_config,
)
from services.integration.github.credentials import (
    GitHubCredentialResolver,
    GitHubRepoCredential,
    UnconfiguredRepositoryError,
    parse_github_config,
)
from services.operator_remedy import NEXT_STEP_PREFIX


def _config() -> dict:
    return {
        "hosts": {
            "github-com": {
                "api_url": "https://api.github.com",
                "git_base_url": "https://github.com",
            },
            "enterprise": {
                "api_url": "https://ghe.corp/api/v3",
                "git_base_url": "https://ghe.corp",
            },
        },
        "default": {
            "host": "github-com",
            "owner": "org-a",
            "token_ref": "TOKEN_A",
        },
    }


def _resolver(raw: dict, allowlist: set[str], secrets: dict[str, str]) -> GitHubCredentialResolver:
    return GitHubCredentialResolver(parse_github_config(raw), allowlist, EnvSecretResolver(secrets))


def test_default_resolves_any_allowlisted_repo_without_enumeration():
    resolver = _resolver(_config(), {"repo-x", "repo-y"}, {"TOKEN_A": "aaa"})

    for repo in ("repo-x", "repo-y"):
        assert resolver.resolve(repo) == GitHubRepoCredential(
            api_url="https://api.github.com",
            owner="org-a",
            git_base_url="https://github.com",
            token="aaa",
        )
    assert resolver.repos() == {"repo-x", "repo-y"}


def test_non_allowlisted_repo_resolves_none():
    resolver = _resolver(_config(), {"repo-x"}, {"TOKEN_A": "aaa"})

    assert resolver.resolve("repo-y") is None
    assert resolver.repos() == {"repo-x"}


def test_per_repo_token_on_the_same_host_and_owner():
    raw = _config()
    raw["repositories"] = {"billing": {"token_ref": "TOKEN_BILLING"}}
    resolver = _resolver(raw, {"svc", "billing"}, {"TOKEN_A": "aaa", "TOKEN_BILLING": "bbb"})

    svc = resolver.resolve("svc")
    billing = resolver.resolve("billing")

    assert svc is not None and billing is not None
    assert svc.owner == billing.owner == "org-a"
    assert svc.git_base_url == billing.git_base_url == "https://github.com"
    assert svc.token == "aaa"
    assert billing.token == "bbb"


def test_per_repo_owner_on_github_com():
    raw = _config()
    raw["repositories"] = {"partner": {"owner": "partner-org"}}
    resolver = _resolver(raw, {"partner"}, {"TOKEN_A": "aaa"})

    cred = resolver.resolve("partner")

    assert cred == GitHubRepoCredential(
        api_url="https://api.github.com",
        owner="partner-org",
        git_base_url="https://github.com",
        token="aaa",
    )


def test_enterprise_and_github_com_coexist():
    raw = _config()
    raw["repositories"] = {
        "legacy": {"host": "enterprise", "owner": "platform", "token_ref": "GHES_TOKEN"}
    }
    resolver = _resolver(raw, {"svc", "legacy"}, {"TOKEN_A": "aaa", "GHES_TOKEN": "ghs"})

    svc = resolver.resolve("svc")
    legacy = resolver.resolve("legacy")

    assert svc == GitHubRepoCredential(
        api_url="https://api.github.com",
        owner="org-a",
        git_base_url="https://github.com",
        token="aaa",
    )
    assert legacy == GitHubRepoCredential(
        api_url="https://ghe.corp/api/v3",
        owner="platform",
        git_base_url="https://ghe.corp",
        token="ghs",
    )


def test_no_default_and_incomplete_repo_fails_closed():
    raw = _config()
    del raw["default"]
    raw["repositories"] = {"orphan": {"token_ref": "TOKEN_A"}}
    resolver = _resolver(raw, {"orphan"}, {"TOKEN_A": "aaa"})

    with pytest.raises(UnconfiguredRepositoryError):
        resolver.resolve("orphan")


def test_missing_secret_reference_fails_closed():
    resolver = _resolver(_config(), {"repo-x"}, {})

    with pytest.raises(SecretNotFoundError):
        resolver.resolve("repo-x")


def test_config_and_secrets_manager_seam_return_the_same_shape():
    class FakeSecretsManager:
        def __init__(self, values: dict[str, str]) -> None:
            self._values = values

        def resolve(self, ref: str) -> str:
            return self._values[ref]

    config = parse_github_config(_config())
    from_env = GitHubCredentialResolver(
        config, {"repo-x"}, EnvSecretResolver({"TOKEN_A": "shared"})
    )
    from_sm = GitHubCredentialResolver(
        config, {"repo-x"}, FakeSecretsManager({"TOKEN_A": "shared"})
    )

    assert from_env.resolve("repo-x") == from_sm.resolve("repo-x")


def test_parse_rejects_default_referencing_unknown_host():
    raw = _config()
    raw["default"]["host"] = "nope"
    with pytest.raises(ConfigError):
        parse_github_config(raw)


def test_parse_rejects_repository_referencing_unknown_host():
    raw = _config()
    raw["repositories"] = {"svc": {"host": "nope"}}
    with pytest.raises(ConfigError):
        parse_github_config(raw)


def test_parse_rejects_host_missing_fields():
    raw = {"hosts": {"github-com": {"api_url": "https://api.github.com"}}}
    with pytest.raises(ConfigError):
        parse_github_config(raw)


def test_parse_accepts_empty_config():
    config = parse_github_config({})
    assert config.hosts == {}
    assert config.default is None
    assert config.repositories == {}


def test_load_env_config_reads_yaml(tmp_path):
    env_dir = tmp_path / "config" / "local"
    env_dir.mkdir(parents=True)
    (env_dir / "github.yaml").write_text("hosts: {}\n", encoding="utf-8")

    loaded = load_env_config("github", environment="local", root=tmp_path)

    assert loaded == {"hosts": {}}


def test_load_env_config_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_env_config("github", environment="local", root=tmp_path)


def test_load_platform_config_reads_tracked_yaml(tmp_path):
    platform_dir = tmp_path / "config" / "platform"
    platform_dir.mkdir(parents=True)
    (platform_dir / "litellm-config.yaml").write_text("model_list: []\n", encoding="utf-8")

    assert load_platform_config("litellm-config", root=tmp_path) == {"model_list": []}


def test_load_platform_config_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_platform_config("litellm-config", root=tmp_path)


def test_repository_platform_config_holds_the_gateway_catalog():
    assert "model_list" in load_platform_config("litellm-config")


def test_env_secret_resolver_reads_dotenv(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("TOKEN_A=from-dotenv\n", encoding="utf-8")
    resolver = EnvSecretResolver(env_file=env_file)

    assert resolver.resolve("TOKEN_A") == "from-dotenv"


def test_a_missing_secret_names_the_variable_the_env_file_and_the_next_command(tmp_path):
    """An operator has to learn which name to set and where, not only that one is absent."""
    env_file = tmp_path / ".env"
    env_file.write_text("OTHER=value\n", encoding="utf-8")

    with pytest.raises(SecretNotFoundError) as caught:
        EnvSecretResolver(env_file=env_file).resolve("GITHUB_TOKEN")

    condition = caught.value.condition()
    assert "GITHUB_TOKEN" in condition
    assert str(env_file) in condition
    assert "inv local-up" not in condition

    message = str(caught.value)
    assert message == f"{condition}\n{NEXT_STEP_PREFIX} {caught.value.remedy}"
    assert "inv local-up" in message


def test_committed_example_template_is_valid_and_resolves():
    example = Path(__file__).resolve().parents[2] / "config" / "local" / "github.yaml.example"
    raw = yaml.safe_load(example.read_text(encoding="utf-8"))
    resolver = GitHubCredentialResolver(
        parse_github_config(raw), {"sample-repo"}, EnvSecretResolver({"GITHUB_TOKEN": "tok"})
    )

    cred = resolver.resolve("sample-repo")

    assert cred is not None
    assert cred.token == "tok"
    assert cred.git_base_url == "https://github.com"
    assert resolver.resolve("not-an-allowlisted-repo") is None
