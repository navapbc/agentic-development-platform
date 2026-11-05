"""Deterministic text the platform writes into a target repository's commit and pull request."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_HEADING = re.compile(r"^(#{1,3})\s+(.+)$")
_TOKEN = re.compile(r"\{[^{}]*\}")

_TEMPLATE_LOCATIONS = (
    ".github/PULL_REQUEST_TEMPLATE.md",
    ".github/pull_request_template.md",
    "docs/PULL_REQUEST_TEMPLATE.md",
    "docs/pull_request_template.md",
    "PULL_REQUEST_TEMPLATE.md",
    "pull_request_template.md",
)

_TICKET = "ticket"
_CHANGES = "changes"
_CONTEXT = "context"
_TESTING = "testing"

_CATEGORIES = (
    (_TICKET, ("ticket", "issue", "jira", "story")),
    (_CONTEXT, ("context", "background", "motivation")),
    (_CHANGES, ("change", "what", "description", "summary")),
    (_TESTING, ("test", "verification", "validation")),
)

_TICKET_TOKEN_WORDS = ("ticket", "issue", "story", "key")
_TICKET_TOKEN_EXCLUSIONS = ("sub", "parent", "epic", "related", "linked")

_UNPOPULATED = "_None supplied by the platform._"

_DEFAULT_SUMMARY = "Apply bounded agent change"
_SUBJECT_LIMIT = 72

_FALLBACK_TEMPLATE = """## Ticket

## Changes

## Context for reviewers

## Testing
"""


@dataclass(frozen=True)
class PullRequestNarrative:
    issue_key: str
    execution_id: str
    summary: str
    description: str
    testing: str


@dataclass(frozen=True)
class _Section:
    heading: str
    lines: list[str]


def compose_pull_request_body(template: str, narrative: PullRequestNarrative) -> str:
    source = template if template.strip() else _FALLBACK_TEMPLATE
    preamble, sections = _split(source)
    filled = [_fill(section, narrative) for section in sections]
    blocks = [block for block in (_render(preamble), *filled) if block]
    return "\n\n".join(blocks) + "\n"


def read_pull_request_template(root: str) -> str:
    for location in _TEMPLATE_LOCATIONS:
        candidate = Path(root) / location
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    return ""


def compose_testing_evidence(*, reviewed: bool, policy_evaluated: bool) -> str:
    evidence = ["Repository checks passed in the platform pipeline."]
    if reviewed:
        evidence.append("Automated plan and code review passed.")
    if policy_evaluated:
        evidence.append("Policy evaluation allowed the change.")
    evidence.append("No manual verification has been performed.")
    return " ".join(evidence)


def compose_commit_message(*, issue_key: str, summary: str, description: str) -> str:
    prefix = f"{issue_key}: " if issue_key else ""
    subject = _elide(f"{prefix}{summary.strip() or _DEFAULT_SUMMARY}", _SUBJECT_LIMIT)
    body = description.strip()
    return f"{subject}\n\n{body}" if body else subject


def _elide(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[: limit - 3].rstrip()}..."


def _split(template: str) -> tuple[list[str], list[_Section]]:
    preamble: list[str] = []
    sections: list[_Section] = []
    for line in template.splitlines():
        match = _HEADING.match(line)
        if match:
            sections.append(_Section(heading=line, lines=[]))
        elif sections:
            sections[-1].lines.append(line)
        else:
            preamble.append(line)
    return preamble, sections


def _fill(section: _Section, narrative: PullRequestNarrative) -> str:
    category = _categorise(section.heading)
    lines = section.lines
    resolved = False
    if category == _TICKET:
        lines, resolved = _resolve_ticket_tokens(lines, narrative.issue_key)
    kept = [line for line in lines if not _is_placeholder(line)]
    content = _content(category, narrative, ticket_resolved=resolved)
    blocks = [block for block in (*content, _render(kept)) if block]
    body = "\n\n".join(blocks)
    return f"{section.heading}\n\n{body or _UNPOPULATED}"


def _categorise(heading: str) -> str:
    title = _HEADING.match(heading).group(2).lower()  # type: ignore[union-attr]
    for category, keywords in _CATEGORIES:
        if any(keyword in title for keyword in keywords):
            return category
    return ""


def _content(category: str, narrative: PullRequestNarrative, *, ticket_resolved: bool) -> list[str]:
    if category == _TICKET:
        return [] if ticket_resolved else [narrative.issue_key]
    if category == _CHANGES:
        return [narrative.summary]
    if category == _CONTEXT:
        provenance = (
            f"Opened by the agentic development platform for {narrative.issue_key}, "
            f"execution {narrative.execution_id}."
        )
        return [line for line in (narrative.description, provenance) if line]
    if category == _TESTING:
        return [narrative.testing]
    return []


def _resolve_ticket_tokens(lines: list[str], issue_key: str) -> tuple[list[str], bool]:
    if not issue_key:
        return lines, False
    resolved = False
    out: list[str] = []
    for line in lines:
        replaced = _TOKEN.sub(lambda match: _substitute(match.group(0), issue_key), line)
        resolved = resolved or replaced != line
        out.append(replaced)
    return out, resolved


def _substitute(token: str, issue_key: str) -> str:
    text = token.strip("{}").lower()
    if any(word in text for word in _TICKET_TOKEN_EXCLUSIONS):
        return token
    if any(word in text for word in _TICKET_TOKEN_WORDS):
        return issue_key
    return token


def _is_placeholder(line: str) -> bool:
    stripped = line.strip()
    if stripped.startswith("("):
        return True
    return any(_is_placeholder_token(token) for token in _TOKEN.findall(stripped))


def _is_placeholder_token(token: str) -> bool:
    text = token.strip("{}")
    return any(character.isalpha() for character in text) and text.upper() == text


def _render(lines: list[str]) -> str:
    return "\n".join(lines).strip()
