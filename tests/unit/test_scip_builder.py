"""Unit tests for the language-keyed SCIP indexer selection and invocation shape."""

from __future__ import annotations

import os
from pathlib import Path

from services.repository_intelligence.indexing.scip_builder import (
    SCIP_PYTHON_INDEXER,
    SCIP_TYPESCRIPT_INDEXER,
    scip_indexer_for,
)
from services.repository_intelligence.languages import Language


def test_scip_indexer_for_maps_supported_languages():
    assert scip_indexer_for(Language.PYTHON) is SCIP_PYTHON_INDEXER
    assert scip_indexer_for(Language.TYPESCRIPT) is SCIP_TYPESCRIPT_INDEXER
    assert scip_indexer_for(Language.JAVASCRIPT) is SCIP_TYPESCRIPT_INDEXER


def test_scip_indexer_for_returns_none_without_an_indexer():
    assert scip_indexer_for(Language.RUBY) is None
    assert scip_indexer_for(Language.TERRAFORM) is None
    assert scip_indexer_for(Language.UNKNOWN) is None


def test_python_indexer_argv_uses_project_flags_and_quiet():
    argv = SCIP_PYTHON_INDEXER.index_argv("proj", "abc1234", Path("/out/index.scip"))

    assert "index" in argv
    assert argv[argv.index("--project-name") + 1] == "proj"
    assert argv[argv.index("--project-version") + 1] == "abc1234"
    assert argv[argv.index("--output") + 1] == "/out/index.scip"
    assert "--quiet" in argv


def test_typescript_indexer_argv_omits_project_flags():
    argv = SCIP_TYPESCRIPT_INDEXER.index_argv("proj", "abc1234", Path("/out/index.scip"))

    assert "--project-name" not in argv
    assert "--quiet" not in argv
    assert argv[-2:] == ["--output", "/out/index.scip"]
    assert "scip-typescript@0.3.14" in argv[2]


def test_typescript_indexer_infers_tsconfig_when_missing(tmp_path):
    argv = SCIP_TYPESCRIPT_INDEXER.index_argv("proj", "abc1234", Path("/out/index.scip"), tmp_path)

    assert "--infer-tsconfig" in argv


def test_typescript_indexer_keeps_existing_tsconfig(tmp_path):
    (tmp_path / "tsconfig.json").write_text("{}", encoding="utf-8")

    argv = SCIP_TYPESCRIPT_INDEXER.index_argv("proj", "abc1234", Path("/out/index.scip"), tmp_path)

    assert "--infer-tsconfig" not in argv


def test_python_indexer_env_injects_venv_when_present(tmp_path):
    (tmp_path / ".venv" / "bin").mkdir(parents=True)

    env = SCIP_PYTHON_INDEXER.index_env(tmp_path)

    assert env["VIRTUAL_ENV"] == str(tmp_path / ".venv")
    assert env["PATH"].split(os.pathsep)[0] == str(tmp_path / ".venv" / "bin")


def test_typescript_indexer_env_leaves_path_untouched(tmp_path):
    (tmp_path / ".venv" / "bin").mkdir(parents=True)

    env = SCIP_TYPESCRIPT_INDEXER.index_env(tmp_path)

    assert "VIRTUAL_ENV" not in env or env.get("VIRTUAL_ENV") == os.environ.get("VIRTUAL_ENV")
    assert env["PATH"] == os.environ.get("PATH", "")
