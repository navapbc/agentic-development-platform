"""Unit tests for repository language detection."""

from __future__ import annotations

from pathlib import Path

from services.repository_intelligence.languages import (
    Language,
    detect_languages,
    primary_language,
)


def _write(root: Path, relative: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")


def test_primary_language_is_python_for_a_python_repo(tmp_path: Path) -> None:
    _write(tmp_path, "app/main.py")
    _write(tmp_path, "app/models.pyi")
    _write(tmp_path, "README.md")
    assert primary_language(tmp_path) is Language.PYTHON


def test_primary_language_is_unknown_for_an_empty_repo(tmp_path: Path) -> None:
    _write(tmp_path, "README.md")
    assert primary_language(tmp_path) is Language.UNKNOWN


def test_detect_languages_orders_by_descending_file_count(tmp_path: Path) -> None:
    _write(tmp_path, "a.java")
    _write(tmp_path, "b.java")
    _write(tmp_path, "c.java")
    _write(tmp_path, "d.ts")
    _write(tmp_path, "e.ts")
    _write(tmp_path, "f.py")
    assert detect_languages(tmp_path) == (
        Language.JAVA,
        Language.TYPESCRIPT,
        Language.PYTHON,
    )


def test_detect_languages_ignores_dependency_directories(tmp_path: Path) -> None:
    _write(tmp_path, "src/app.py")
    _write(tmp_path, "node_modules/pkg/index.js")
    _write(tmp_path, "vendor/lib/thing.php")
    _write(tmp_path, ".venv/lib/site.py")
    assert detect_languages(tmp_path) == (Language.PYTHON,)


def test_detect_languages_tie_break_is_deterministic(tmp_path: Path) -> None:
    _write(tmp_path, "a.rb")
    _write(tmp_path, "b.tf")
    assert detect_languages(tmp_path) == (Language.RUBY, Language.TERRAFORM)
