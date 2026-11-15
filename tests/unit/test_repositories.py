"""The repository registry read from config/<env>/repositories.yaml."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from scripts import repositories_compose
from scripts.config import RemovedSettingError, refuse_removed_settings
from services.integration.credentials import ConfigError
from services.integration.repositories import (
    GITHUB,
    LOCAL,
    UnknownRepositoryError,
    load_repository_registry,
    parse_repositories,
)
from tests.support import configure_repositories, hermetic_settings

REGISTRY = {
    "defaults": {"default_branch": "trunk", "index": True},
    "repositories": {
        "svc": {"path": "../svc", "index_ref": "abc123"},
        "partner": {"owner": "partner-org", "index": False},
        "notes": {"backend": "local", "path": "/srv/notes", "default_branch": "main"},
    },
}


def _registry(raw=REGISTRY, *, host_base: Path | None = None):
    return parse_repositories(raw, root="/repos", host_base=host_base)


def test_each_repository_takes_the_defaults_unless_it_sets_its_own() -> None:
    registry = _registry()

    assert registry.names() == {"svc", "partner", "notes"}
    assert registry.names(GITHUB) == {"svc", "partner"}
    assert registry.names(LOCAL) == {"notes"}
    assert registry.require("svc").default_branch == "trunk"
    assert registry.require("notes").default_branch == "main"
    assert registry.require("partner").owner == "partner-org"


def test_indexed_repositories_appear_under_the_repository_root() -> None:
    assert _registry().indexed() == {
        "notes": ("/repos/notes", None),
        "svc": ("/repos/svc", "abc123"),
    }


def test_on_the_host_a_repository_with_a_path_is_found_at_that_path(tmp_path: Path) -> None:
    registry = _registry(host_base=tmp_path)

    assert registry.location("svc") == str((tmp_path.parent / "svc").resolve())
    assert registry.location("notes") == "/srv/notes"
    assert registry.location("partner") == "/repos/partner"


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"backend": "svn"}, "is not one of github, local"),
        ({"backend": "local"}, "names no path"),
        ({"backend": "local", "path": "/x", "owner": "o"}, "only a GitHub repository takes"),
        ({"index": "yes"}, "is not true or false"),
        ({"branch": "main"}, "unknown fields: branch"),
    ],
)
def test_a_malformed_entry_is_refused(entry: dict, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        _registry({"repositories": {"svc": entry}})


def test_an_unknown_repository_is_refused() -> None:
    with pytest.raises(UnknownRepositoryError, match="'other' is not configured"):
        _registry().backend_for("other")


def test_the_registry_is_read_from_the_environment_config(monkeypatch, tmp_path: Path) -> None:
    configure_repositories(monkeypatch, tmp_path, "repositories:\n  svc: {}\n")

    registry = load_repository_registry(hermetic_settings(repository_root="/repositories"))

    assert registry.names() == {"svc"}
    assert registry.root == "/repositories"


def test_a_removed_setting_names_the_file_that_replaced_it(monkeypatch, tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("SOURCE_CONTROL_BACKEND=local\n", encoding="utf-8")
    monkeypatch.setenv("REPO_INTELLIGENCE_REPOS", "svc=/repos/svc")

    with pytest.raises(RemovedSettingError) as raised:
        refuse_removed_settings(env_file)

    assert "REPO_INTELLIGENCE_REPOS, SOURCE_CONTROL_BACKEND are no longer read" in str(raised.value)
    assert "repositories.yaml" in str(raised.value)


def test_the_compose_override_mounts_each_repository_with_a_path(tmp_path: Path) -> None:
    rendered = repositories_compose.render([_registry()], base=tmp_path)
    svc = f"{(tmp_path.parent / 'svc').resolve()}:/repos/svc"

    assert set(rendered["services"]) == {"repo-intelligence", "repo-index-worker"}
    assert rendered["services"]["repo-intelligence"]["volumes"] == [
        "/srv/notes:/repos/notes:ro",
        f"{svc}:ro",
    ]
    assert yaml.safe_load(yaml.safe_dump(rendered)) == rendered


def _tenant_config(root: Path, *, platform: str, beta: str) -> None:
    config = root / "config" / "local"
    (config / "tenants" / "beta").mkdir(parents=True)
    (config / "repositories.yaml").write_text(f"config_version: 1\n{platform}", encoding="utf-8")
    (config / "tenants" / "beta" / "repositories.yaml").write_text(
        f"config_version: 1\n{beta}", encoding="utf-8"
    )


def _local_repository(name: str, path: Path) -> str:
    return f"repositories:\n  {name}:\n    backend: local\n    path: {path}\n"


def test_each_tenant_mounts_its_repositories_under_its_own_directory(tmp_path: Path) -> None:
    _tenant_config(
        tmp_path,
        platform=_local_repository("web", tmp_path / "adp-web"),
        beta=_local_repository("web", tmp_path / "beta-web"),
    )

    registries = repositories_compose.tenant_registries(
        hermetic_settings(), ["adp", "beta"], root=tmp_path
    )
    rendered = repositories_compose.render(registries, base=tmp_path)

    expected = [
        f"{tmp_path / 'adp-web'}:/tenants/adp/repositories/web:ro",
        f"{tmp_path / 'beta-web'}:/tenants/beta/repositories/web:ro",
    ]
    assert rendered["services"]["repo-intelligence"]["volumes"] == expected
    assert rendered["services"]["repo-index-worker"]["volumes"] == expected


def _compose_main(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **settings: object
) -> tuple[int, Path]:
    monkeypatch.setattr(repositories_compose, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(repositories_compose, "get_settings", lambda: hermetic_settings(**settings))
    output = tmp_path / "compose.repositories.yaml"
    return repositories_compose.main(["--output", str(output)]), output


def test_repos_compose_writes_the_mounts_of_every_tenant(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name in ("adp-web", "beta-web"):
        (tmp_path / name).mkdir()
    _tenant_config(
        tmp_path,
        platform=_local_repository("web", tmp_path / "adp-web"),
        beta=_local_repository("web", tmp_path / "beta-web"),
    )

    code, output = _compose_main(monkeypatch, tmp_path, tenants="adp,beta", tenant="adp")

    assert code == 0
    text = output.read_text(encoding="utf-8")
    assert text.startswith(repositories_compose.HEADER)
    assert yaml.safe_load(text)["services"]["repo-index-worker"]["volumes"] == [
        f"{tmp_path / 'adp-web'}:/tenants/adp/repositories/web:ro",
        f"{tmp_path / 'beta-web'}:/tenants/beta/repositories/web:ro",
    ]


def test_repos_compose_refuses_a_tenant_repository_without_its_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "adp-web").mkdir()
    _tenant_config(
        tmp_path,
        platform=_local_repository("web", tmp_path / "adp-web"),
        beta=_local_repository("beta-web", tmp_path / "beta-web"),
    )

    code, output = _compose_main(monkeypatch, tmp_path, tenants="adp,beta", tenant="adp")

    assert code == 1
    assert not output.exists()
    assert f"checkouts not found: beta-web at {tmp_path / 'beta-web'}" in capsys.readouterr().out


def test_repos_compose_without_a_tenant_mounts_the_environment_repositories_at_the_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "web").mkdir()
    repositories = _local_repository("web", tmp_path / "web")
    configure_repositories(monkeypatch, tmp_path / "env", repositories)

    code, output = _compose_main(
        monkeypatch, tmp_path, tenants="", tenant="", repository_root="/repos"
    )

    assert code == 0
    assert yaml.safe_load(output.read_text(encoding="utf-8"))["services"]["repo-intelligence"][
        "volumes"
    ] == [f"{tmp_path / 'web'}:/repos/web:ro"]
