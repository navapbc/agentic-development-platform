"""Derive symbol and file focus terms from a ticket's title and description.

The repository-intelligence context providers key on a symbol or file name. A
ticket title is a sentence, so it never matches a symbol and yields an empty
context package. These helpers pull the code-like identifiers and repository
file paths out of the ticket text so planning can query the index for each and
union the results.
"""

from __future__ import annotations

import re

_CODE_EXTENSIONS = frozenset(
    {
        ".py",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".go",
        ".rb",
        ".php",
        ".java",
        ".kt",
        ".scala",
        ".cs",
        ".rs",
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".hpp",
        ".swift",
        ".vue",
        ".svelte",
    }
)

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_PATH = re.compile(r"[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)+")
_CASE_CHANGE = re.compile(r"[a-z][A-Z]")
_MIN_IDENTIFIER_LENGTH = 3
_DEFAULT_LIMIT = 12


def _is_code_identifier(token: str) -> bool:
    if len(token) < _MIN_IDENTIFIER_LENGTH:
        return False
    if "_" in token:
        return True
    return bool(_CASE_CHANGE.search(token))


def extract_focus_terms(text: str, *, limit: int = _DEFAULT_LIMIT) -> list[str]:
    """Return the code-like identifiers found in the text, in first-seen order."""
    if not text:
        return []
    terms: list[str] = []
    seen: set[str] = set()
    for match in _IDENTIFIER.finditer(text):
        token = match.group(0)
        if _is_code_identifier(token) and token not in seen:
            seen.add(token)
            terms.append(token)
            if len(terms) >= limit:
                break
    return terms


def extract_file_paths(text: str, *, limit: int = _DEFAULT_LIMIT) -> list[str]:
    """Return the repository file paths with a recognized code extension."""
    if not text:
        return []
    paths: list[str] = []
    seen: set[str] = set()
    for match in _PATH.finditer(text):
        token = match.group(0)
        base = token.split("#", 1)[0].split(":", 1)[0]
        dot = base.rfind(".")
        if dot < 0 or base[dot:].lower() not in _CODE_EXTENSIONS:
            continue
        if base not in seen:
            seen.add(base)
            paths.append(base)
            if len(paths) >= limit:
                break
    return paths
