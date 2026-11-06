"""A deployed task receives its environment's config files through PLATFORM_ENV_CONFIG."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from scripts import env_config_files
from services.integration.credentials import env_config_dir, load_env_config

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_the_files_are_written_under_their_names(tmp_path) -> None:
    written = env_config_files.write({"tickets.yaml": "version: 1\n"}, tmp_path / "config")

    assert written == [tmp_path / "config" / "tickets.yaml"]
    assert written[0].read_text(encoding="utf-8") == "version: 1\n"


@pytest.mark.parametrize("name", ["../tickets.yaml", "nested/tickets.yaml", ".env", "tickets.json"])
def test_a_name_outside_the_directory_or_not_yaml_is_refused(tmp_path, name) -> None:
    with pytest.raises(env_config_files.EnvConfigError, match="plain YAML file name"):
        env_config_files.write({name: "x: 1\n"}, tmp_path)

    assert not any(tmp_path.iterdir())


def test_the_loader_reads_the_directory_the_setting_names(tmp_path, monkeypatch) -> None:
    (tmp_path / "tickets.yaml").write_text("backend: rebar\n", encoding="utf-8")
    monkeypatch.setenv("PLATFORM_ENV_CONFIG_DIR", str(tmp_path))

    assert env_config_dir("aws") == tmp_path
    assert load_env_config("tickets", environment="aws") == {"backend": "rebar"}
    assert env_config_dir("aws", root=REPO_ROOT) == REPO_ROOT / "config" / "aws"


def test_the_entrypoint_writes_the_files_and_points_the_loader_at_them(tmp_path) -> None:
    env = {
        "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}",
        "TMPDIR": str(tmp_path),
        "PLATFORM_ENV_CONFIG": json.dumps({"tickets.yaml": "backend: rebar\n"}),
    }
    result = subprocess.run(
        [
            "sh",
            "docker/platform-entrypoint.sh",
            "sh",
            "-c",
            'cat "$PLATFORM_ENV_CONFIG_DIR/tickets.yaml"',
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.splitlines()[-1] == "backend: rebar"
