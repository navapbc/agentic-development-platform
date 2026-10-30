"""The git credential helper hands over a token that never reaches argv, a file, or a repr."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from scripts.config import Settings
from services.integration.credentials import ConfigError
from services.integration.github import factory
from services.integration.github.credentials import GitHubRepoCredential
from services.integration.github.git_auth import CREDENTIAL_ENV, NO_AUTH, GitAuth
from tests.support import hermetic_settings

TOKEN = "ghs-helper-secret"
BASE_URL = "https://acme.ghe"


def _credential(auth: GitAuth, operation: str, request: str, cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *auth.config_args(), "credential", operation],
        input=request,
        cwd=cwd,
        env={**os.environ, **auth.env()},
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout


def test_the_helper_answers_git_with_the_token_from_the_environment(tmp_path: Path) -> None:
    answer = _credential(
        GitAuth(TOKEN, BASE_URL), "fill", "protocol=https\nhost=acme.ghe\n\n", tmp_path
    ).splitlines()

    assert "username=x-access-token" in answer
    assert f"password={TOKEN}" in answer


def test_the_reset_stops_an_inherited_helper_persisting_the_credential(tmp_path: Path) -> None:
    auth = GitAuth(TOKEN, BASE_URL)
    without_reset = tmp_path / "without-reset"
    with_reset = tmp_path / "with-reset"
    approve = f"protocol=https\nhost=acme.ghe\nusername=x-access-token\npassword={TOKEN}\n\n"

    def run(store: Path, config_args: tuple[str, ...]) -> None:
        subprocess.run(
            ["git", "-c", f"credential.helper=store --file={store}", *config_args]
            + ["credential", "approve"],
            input=approve,
            cwd=tmp_path,
            env={**os.environ, **auth.env()},
            check=True,
            capture_output=True,
            text=True,
        )

    run(without_reset, ())
    run(with_reset, auth.config_args())

    assert without_reset.exists()
    assert not with_reset.exists()


def test_the_token_stays_out_of_the_command_and_the_repr() -> None:
    auth = GitAuth(TOKEN, BASE_URL)

    assert TOKEN not in " ".join(auth.config_args())
    assert TOKEN not in repr(auth)
    assert auth.env()[CREDENTIAL_ENV] == TOKEN


def test_without_a_token_git_gets_no_helper_but_still_refuses_to_prompt() -> None:
    assert NO_AUTH.config_args() == ()
    assert NO_AUTH.env() == {"GIT_TERMINAL_PROMPT": "0"}


def test_the_local_backend_resolves_every_repository_to_no_credential() -> None:
    auth_for = factory.build_git_auth_for(hermetic_settings(source_control_backend="local"))

    assert auth_for("agentic-demo-service") == NO_AUTH


def test_the_github_backend_resolves_a_repository_to_its_own_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Resolver:
        def resolve(self, repo: str) -> GitHubRepoCredential | None:
            if repo != "agentic-demo-service":
                return None
            return GitHubRepoCredential(
                api_url="https://acme.ghe/api/v3",
                owner="acme-org",
                git_base_url="https://acme.ghe",
                token=TOKEN,
            )

    monkeypatch.setattr(
        "services.integration.github.credentials.build_github_credential_resolver",
        lambda settings: _Resolver(),
    )
    auth_for = factory.build_git_auth_for(hermetic_settings(source_control_backend="github"))

    assert auth_for("agentic-demo-service").env()[CREDENTIAL_ENV] == TOKEN
    assert auth_for("not-allowlisted") == NO_AUTH


def test_an_unreadable_github_config_resolves_to_no_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _unreadable(settings: Settings) -> None:
        raise ConfigError("no github config for this environment")

    monkeypatch.setattr(
        "services.integration.github.credentials.build_github_credential_resolver", _unreadable
    )
    auth_for = factory.build_git_auth_for(hermetic_settings(source_control_backend="github"))

    assert auth_for("agentic-demo-service") == NO_AUTH


def test_the_helper_refuses_a_host_the_credential_was_not_issued_for(tmp_path: Path) -> None:
    auth = GitAuth(TOKEN, BASE_URL)
    proc = subprocess.run(
        ["git", *auth.config_args(), "credential", "fill"],
        input="protocol=https\nhost=redirect-target.invalid\n\n",
        cwd=tmp_path,
        env={**os.environ, **auth.env()},
        capture_output=True,
        text=True,
    )

    assert proc.returncode != 0
    assert TOKEN not in proc.stdout


def test_a_credential_with_no_host_to_scope_it_offers_nothing() -> None:
    unscoped = GitAuth(TOKEN)

    assert unscoped.config_args() == ()
    assert CREDENTIAL_ENV not in unscoped.env()
