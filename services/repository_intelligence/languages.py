"""Detect the languages present in a repository from its source file extensions."""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Iterator
from enum import Enum
from pathlib import Path


class Language(str, Enum):
    PYTHON = "python"
    JAVA = "java"
    TYPESCRIPT = "typescript"
    JAVASCRIPT = "javascript"
    RUBY = "ruby"
    TERRAFORM = "terraform"
    PHP = "php"
    UNKNOWN = "unknown"


_EXTENSIONS: dict[str, Language] = {
    ".py": Language.PYTHON,
    ".pyi": Language.PYTHON,
    ".java": Language.JAVA,
    ".ts": Language.TYPESCRIPT,
    ".tsx": Language.TYPESCRIPT,
    ".js": Language.JAVASCRIPT,
    ".jsx": Language.JAVASCRIPT,
    ".mjs": Language.JAVASCRIPT,
    ".cjs": Language.JAVASCRIPT,
    ".rb": Language.RUBY,
    ".tf": Language.TERRAFORM,
    ".tfvars": Language.TERRAFORM,
    ".php": Language.PHP,
}

_IGNORED_DIRS = frozenset(
    {
        ".git",
        ".terraform",
        ".mypy_cache",
        ".pytest_cache",
        "__pycache__",
        "node_modules",
        "vendor",
        "venv",
        ".venv",
        "dist",
        "build",
        "target",
    }
)

_MAX_FILES = 5000


def detect_languages(repo_root: str | Path) -> tuple[Language, ...]:
    """Return the repository's languages ordered by descending source file count."""
    counts = _language_counts(repo_root)
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0].value))
    return tuple(language for language, _ in ordered)


def primary_language(repo_root: str | Path) -> Language:
    """Return the most common source language, or UNKNOWN when none is recognized."""
    languages = detect_languages(repo_root)
    return languages[0] if languages else Language.UNKNOWN


def _language_counts(repo_root: str | Path) -> Counter[Language]:
    counts: Counter[Language] = Counter()
    for index, path in enumerate(_iter_source_files(Path(repo_root))):
        if index >= _MAX_FILES:
            break
        language = _EXTENSIONS.get(path.suffix.lower())
        if language is not None:
            counts[language] += 1
    return counts


def _iter_source_files(root: Path) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in _IGNORED_DIRS]
        for name in filenames:
            yield Path(dirpath) / name
