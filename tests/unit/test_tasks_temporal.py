"""Temporal schema setup and the minor-release upgrade path in the invoke task runner."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
import tasks as tasks_module
import yaml
from invoke import Exit
from invoke.context import Context
from invoke.runners import Result

COMPOSE = f"{tasks_module.COMPOSE} -f compose.yaml"
BUILD_ID = "0.3.0.20261028093000"


class _DatabaseContext(Context):
    """Answers the schema_version queries from a fixed map and records every other command."""

    def __init__(self, versions: dict[str, str | None]) -> None:
        super().__init__()
        self.versions = versions
        self.commands: list[tuple[str, dict[str, str]]] = []

    def run(self, command: str, **kwargs: object) -> Result:  # pyright: ignore[reportIncompatibleMethodOverride]
        if "psql" in command:
            database = next(name for name in ("temporal_visibility", "temporal") if name in command)
            version = self.versions.get(database)
            if "to_regclass" in command:
                return Result(stdout="schema_version" if version else "")
            return Result(stdout=version or "")
        env = kwargs.get("env") or {}
        assert isinstance(env, dict)
        self.commands.append((command, env))
        return Result()


@pytest.fixture(autouse=True)
def fixed_build_id(monkeypatch):
    """Hold the build identifier still so `local_up` neither digests the sources nor reaches
    Temporal. A matching recorded identifier means this build replaces no worker, which is the
    case these tests exercise."""
    monkeypatch.setattr(tasks_module, "resolve_build_id", lambda: BUILD_ID)
    monkeypatch.setattr(tasks_module, "recorded_build_id", lambda: BUILD_ID)
    monkeypatch.setattr(tasks_module, "_use_hardened_base", lambda: None)
    monkeypatch.setattr(tasks_module, "_report_exceptions_setup", lambda: None)


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("COMPOSE_FILE", raising=False)
    return tmp_path


def _versions(steps: list[tasks_module.TemporalRelease]) -> list[str]:
    return [release.version for release in steps]


@pytest.mark.parametrize(
    ("schema", "visibility", "expected"),
    [
        ((1, 18), (1, 9), ["1.30.8", "1.31.4", "1.32.2"]),
        ((1, 18), (1, 13), ["1.31.4", "1.32.2"]),
        ((1, 19), (1, 14), ["1.32.2"]),
    ],
)
def test_upgrade_runs_each_minor_release_after_the_current_one(schema, visibility, expected):
    assert _versions(tasks_module._temporal_upgrade_steps(schema, visibility)) == expected


@pytest.mark.parametrize(
    ("schema", "visibility"), [((1, 17), (1, 8)), ((1, 18), (1, 12)), ((1, 20), (1, 15))]
)
def test_upgrade_refuses_a_schema_off_the_path(schema, visibility):
    with pytest.raises(Exit, match="No upgrade path"):
        tasks_module._temporal_upgrade_steps(schema, visibility)


def test_only_the_intermediate_releases_override_the_compose_builds():
    *intermediate, target = tasks_module._temporal_upgrade_steps((1, 18), (1, 9))

    assert target.compose_override() == ""
    for release in intermediate:
        override = yaml.safe_load(release.compose_override().replace("!reset null", "null"))
        services = override["services"]
        pin = f":{release.version}@sha256:"
        assert services["temporal"]["image"].startswith(f"docker.io/temporalio/server{pin}")
        assert services["temporal-admin-tools"]["image"].startswith(
            f"docker.io/temporalio/admin-tools{pin}"
        )
        assert "build: !reset null" in release.compose_override()
        assert services["temporal"]["healthcheck"]["test"][:2] == ["CMD", "wget"]


def test_the_target_matches_the_release_the_dockerfiles_build():
    from scripts.publish_temporal_images import source_version

    root = Path(tasks_module.__file__).parent
    for name in ("temporal.Dockerfile", "temporal-admin-tools.Dockerfile"):
        dockerfile = (root / "docker" / name).read_text(encoding="utf-8")
        assert source_version(dockerfile) == tasks_module.TEMPORAL_TARGET.version


def test_local_up_starts_compose_then_the_cluster_with_credentials_and_repositories(project):
    context = _DatabaseContext({"temporal": None, "temporal_visibility": None})

    tasks_module.local_up(context)

    assert [command for command, _ in context.commands] == [
        f"{tasks_module.HOST_PYTHON} -m scripts.repositories_compose",
        f"{COMPOSE} build --provenance=false --build-arg AGENT_BUILD_ID={BUILD_ID}",
        f"{COMPOSE} run --rm --build db-migrate",
        f"{COMPOSE} run --rm --build temporal-admin-tools schema",
        f"{COMPOSE} up -d --wait --wait-timeout 600 temporal",
        f"{COMPOSE} run --rm temporal-admin-tools namespace",
        f"{COMPOSE} up -d --wait --wait-timeout 600 --remove-orphans",
        f"{tasks_module.HOST_PYTHON} -m scripts.k8s_cluster reload --build-id {BUILD_ID} "
        "--compose-file compose.yaml",
        f"{tasks_module.HOST_PYTHON} -m scripts.aws_login",
        f"{tasks_module.HOST_PYTHON} -m scripts.repos_sync",
    ]


def test_local_up_accepts_a_database_at_the_target_schema(project):
    context = _DatabaseContext({"temporal": "1.19", "temporal_visibility": "1.14"})

    tasks_module.local_up(context)

    stack = [command for command, _ in context.commands if command.startswith(COMPOSE)]
    assert stack[-1] == f"{COMPOSE} up -d --wait --wait-timeout 600 --remove-orphans"


def test_local_up_refuses_to_skip_minor_releases(project):
    context = _DatabaseContext({"temporal": "1.18", "temporal_visibility": "1.9"})

    with pytest.raises(Exit, match="Run inv temporal-upgrade"):
        tasks_module.local_up(context)

    assert not any("temporal-admin-tools" in command for command, _ in context.commands)


def test_local_up_refuses_a_schema_newer_than_the_target(project):
    context = _DatabaseContext({"temporal": "1.20", "temporal_visibility": "1.14"})

    with pytest.raises(Exit, match="newer than server"):
        tasks_module.local_up(context)


def test_temporal_upgrade_walks_each_release_with_its_images(project, monkeypatch):
    context = _DatabaseContext({"temporal": "1.18", "temporal_visibility": "1.9"})
    overrides: list[str] = []
    real_run = context.run

    def run(command, **kwargs):
        compose_file = (kwargs.get("env") or {}).get("COMPOSE_FILE", "")
        if compose_file:
            override = compose_file.split(os.pathsep)[-1]
            overrides.append(Path(override).read_text(encoding="utf-8"))
        return real_run(command, **kwargs)

    monkeypatch.setattr(context, "run", run)

    tasks_module.temporal_upgrade(context, skip_backup=True)

    bare = tasks_module.COMPOSE
    server_up = "up -d --build --no-deps --wait --wait-timeout 600 temporal"
    assert [command for command, _ in context.commands] == [
        f"{COMPOSE} up -d --wait postgres",
        *[f"{bare} run --rm temporal-admin-tools schema", f"{bare} {server_up}"] * 2,
        f"{COMPOSE} run --rm --build temporal-admin-tools schema",
        f"{COMPOSE} {server_up}",
        f"{COMPOSE} run --rm temporal-admin-tools namespace",
    ]
    files = [env.get("COMPOSE_FILE", "") for _, env in context.commands]
    assert [entry.split(os.pathsep)[0] for entry in files if entry] == ["compose.yaml"] * 4
    images = [
        line.split()[-1] for text in overrides for line in text.splitlines() if "image:" in line
    ]
    assert [image.partition("@")[0] for image in images[::2]] == [
        "docker.io/temporalio/server:1.30.8",
        "docker.io/temporalio/server:1.30.8",
        "docker.io/temporalio/server:1.31.4",
        "docker.io/temporalio/server:1.31.4",
    ]
    assert not list(Path(tempfile.gettempdir()).glob("temporal-1.3*-*.yaml"))


def test_temporal_upgrade_backs_up_both_databases_before_any_schema_change(project, tmp_path):
    context = _DatabaseContext({"temporal": "1.19", "temporal_visibility": "1.14"})
    backups = tmp_path / "backup"

    tasks_module.temporal_upgrade(context, backup_dir=str(backups))

    commands = [command for command, _ in context.commands]
    dumps = [command for command in commands if "pg_dump" in command]
    assert [dump.rsplit("> ", 1)[1] for dump in dumps] == [
        str(backups / "temporal.dump"),
        str(backups / "temporal_visibility.dump"),
    ]
    assert commands.index(dumps[-1]) < commands.index(
        f"{COMPOSE} run --rm --build temporal-admin-tools schema"
    )
    assert backups.is_dir()


def test_temporal_upgrade_refuses_an_empty_database(project):
    context = _DatabaseContext({"temporal": None, "temporal_visibility": None})

    with pytest.raises(Exit, match="inv local-up"):
        tasks_module.temporal_upgrade(context, skip_backup=True)
