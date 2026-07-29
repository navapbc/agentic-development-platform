"""Unit tests for the build-metadata repository-intelligence provider."""

from __future__ import annotations

from pathlib import Path

from services.repository_intelligence.normalization.models import RepoRef, Source
from services.repository_intelligence.providers.build_metadata.build_metadata_provider import (
    BuildMetadataProvider,
    BuildTarget,
    canonical_name,
)

_PROJECT = "agentic-demo-service"


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _requirements_corpus(root: Path) -> None:
    _write(
        root / "pyproject.toml",
        '[tool.pytest.ini_options]\npythonpath = ["."]\ntestpaths = ["tests"]\n',
    )
    _write(root / "requirements.txt", "fastapi>=0.110\nuvicorn[standard]>=0.29\n")
    _write(
        root / "requirements-dev.txt",
        "-r requirements.txt\npytest>=8.0\nhttpx>=0.27  # client\nruff>=0.5\nbandit>=1.7\n",
    )


def test_canonical_name_applies_pep503_normalization():
    assert canonical_name("My_Pkg.Name") == "my-pkg-name"
    assert canonical_name("uvicorn") == "uvicorn"


def test_requirements_dependencies_capture_external_packages(tmp_path: Path):
    _requirements_corpus(tmp_path)
    provider = BuildMetadataProvider(tmp_path, project=_PROJECT)
    repo = RepoRef(repo=_PROJECT, commit_sha="head")

    edges = provider.repo_get_module_dependencies(repo)

    assert [(e.to_module, e.kind) for e in edges] == [
        ("bandit", "dev_dependency"),
        ("fastapi", "runtime_dependency"),
        ("httpx", "dev_dependency"),
        ("pytest", "dev_dependency"),
        ("ruff", "dev_dependency"),
        ("uvicorn", "runtime_dependency"),
    ]
    assert all(e.from_module == _PROJECT for e in edges)
    assert all(e.provenance.source is Source.BUILD_METADATA for e in edges)
    assert all(e.provenance.confidence == 1.0 for e in edges)


def test_runtime_include_precedence_over_dev_classification(tmp_path: Path):
    _requirements_corpus(tmp_path)
    provider = BuildMetadataProvider(tmp_path, project=_PROJECT)
    repo = RepoRef(repo=_PROJECT, commit_sha="head")

    edges = provider.repo_get_module_dependencies(repo)
    by_name = {e.to_module: e.kind for e in edges}

    assert by_name["fastapi"] == "runtime_dependency"
    assert by_name["uvicorn"] == "runtime_dependency"
    assert sum(1 for e in edges if e.to_module == "fastapi") == 1


def test_module_filter_scopes_to_the_project(tmp_path: Path):
    _requirements_corpus(tmp_path)
    provider = BuildMetadataProvider(tmp_path, project=_PROJECT)
    repo = RepoRef(repo=_PROJECT, commit_sha="head")

    assert provider.repo_get_module_dependencies(repo, _PROJECT)
    assert provider.repo_get_module_dependencies(repo, "some.internal.module") == []


def test_build_targets_from_pytest_config(tmp_path: Path):
    _requirements_corpus(tmp_path)
    provider = BuildMetadataProvider(tmp_path, project=_PROJECT)

    assert provider.build_targets() == [BuildTarget(name="tests", kind="test", value="tests")]


def test_pyproject_project_table_dependencies_and_targets(tmp_path: Path):
    _write(
        tmp_path / "pyproject.toml",
        "[build-system]\n"
        'requires = ["hatchling"]\n'
        'build-backend = "hatchling.build"\n\n'
        "[project]\n"
        'name = "demo"\n'
        'dependencies = ["fastapi>=0.1", "httpx"]\n\n'
        "[project.optional-dependencies]\n"
        'dev = ["pytest>=8.0"]\n\n'
        "[project.scripts]\n"
        'demo = "demo.cli:main"\n',
    )
    provider = BuildMetadataProvider(tmp_path, project="demo")
    repo = RepoRef(repo="demo", commit_sha="head")

    edges = provider.repo_get_module_dependencies(repo)
    targets = provider.build_targets()

    assert [(e.to_module, e.kind) for e in edges] == [
        ("fastapi", "runtime_dependency"),
        ("hatchling", "build_dependency"),
        ("httpx", "runtime_dependency"),
        ("pytest", "optional_dependency:dev"),
    ]
    assert targets == [
        BuildTarget(name="hatchling.build", kind="build_backend", value="hatchling.build"),
        BuildTarget(name="demo", kind="console_script", value="demo.cli:main"),
    ]


def test_missing_build_files_yield_no_dependencies(tmp_path: Path):
    provider = BuildMetadataProvider(tmp_path, project=_PROJECT)
    repo = RepoRef(repo=_PROJECT, commit_sha="head")

    assert provider.repo_get_module_dependencies(repo) == []
    assert provider.build_targets() == []
