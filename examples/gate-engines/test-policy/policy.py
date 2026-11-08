"""Report source a change modifies without a matching test change, as review-json.

The platform runs `bin/review --against <ref> --json-out <file>` in the checkout. A deterministic
step decides the findings from the path mappings in the TOML rules file. When there are findings
and the platform has injected its model gateway, one call asks the model for suggested test cases,
which go only into each finding's detail. A failed or slow call leaves a fixed detail, so the
findings and the gate result do not depend on the model. The exit status is 0 with or without
findings and 2 for a rules file the engine cannot load.
"""

from __future__ import annotations

import argparse
import fnmatch
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

TOOL = "test-policy"
SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW")
MODEL_TIMEOUT_SECONDS = 60
GIT_TIMEOUT_SECONDS = 120
MAX_DIFF_CHARS_PER_FILE = 6_000
MAX_SUGGESTIONS = 5
FIXED_DETAIL = (
    "The change modifies this file and changes no test that the rules map to it. Add or update "
    "a test that exercises the changed behavior."
)
SYSTEM_PROMPT = (
    "You suggest unit test cases for source changes that arrived without tests. Reply with one "
    "JSON object only. Its keys are the file paths you were given, and each value is a list of at "
    f"most {MAX_SUGGESTIONS} short test case descriptions for the changed behavior in that file."
)
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


class RulesError(ValueError):
    """The rules file is missing, malformed, or names an unknown severity."""


@dataclass(frozen=True)
class PathMapping:
    name: str
    sources: tuple[str, ...]
    exclude: tuple[str, ...]
    tests: tuple[str, ...]
    severity: str

    def covers(self, path: str) -> bool:
        return any(fnmatch.fnmatch(path, p) for p in self.sources) and not any(
            fnmatch.fnmatch(path, p) for p in self.exclude
        )

    def test_globs(self, path: str) -> list[str]:
        stem = glob.escape(PurePosixPath(path).stem)
        return [pattern.replace("{stem}", stem) for pattern in self.tests]


@dataclass(frozen=True)
class Uncovered:
    path: str
    line: int
    mapping: PathMapping


def load_rules(path: Path) -> list[PathMapping]:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RulesError(f"cannot read the rules file {path}: {exc}") from exc
    mappings = []
    for entry in raw.get("mapping", []):
        severity = str(entry.get("severity", "MEDIUM")).upper()
        if severity not in SEVERITIES:
            raise RulesError(f"mapping {entry.get('name')!r} has unknown severity {severity!r}")
        try:
            mappings.append(
                PathMapping(
                    name=entry["name"],
                    sources=tuple(entry["sources"]),
                    exclude=tuple(entry.get("exclude", [])),
                    tests=tuple(entry["tests"]),
                    severity=severity,
                )
            )
        except KeyError as exc:
            raise RulesError(f"mapping {entry.get('name')!r} lacks {exc}") from exc
    return mappings


def _git(args: list[str], cwd: Path) -> str:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is not on PATH")
    return subprocess.run(
        [git, "-c", "core.quotePath=false", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=GIT_TIMEOUT_SECONDS,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    ).stdout


def change_base(against: str, cwd: Path) -> str:
    """Return the merge base of the change, so later commits on the base branch are not counted."""
    try:
        return _git(["merge-base", against, "HEAD"], cwd).strip()
    except subprocess.CalledProcessError:
        return against


def changed_files(base: str, cwd: Path) -> dict[str, int]:
    """Return each added, modified, or untracked file with the first line the change adds."""
    first_line: dict[str, int] = {}
    path = ""
    diff = _git(
        ["diff", "--unified=0", "--no-color", "--no-ext-diff", "--diff-filter=AMR", base], cwd
    )
    for text in diff.splitlines():
        if text.startswith("+++ "):
            path = text[6:] if text.startswith("+++ b/") else ""
        elif (hunk := _HUNK.match(text)) and path and path not in first_line:
            first_line[path] = max(int(hunk.group(1)), 1)
    untracked = _git(["ls-files", "--others", "--exclude-standard", "-z"], cwd)
    for name in filter(None, untracked.split("\0")):
        first_line[name] = 1
    return first_line


def uncovered(mappings: list[PathMapping], changed: Mapping[str, int]) -> list[Uncovered]:
    found = []
    for path, line in sorted(changed.items()):
        mapping = next((m for m in mappings if m.covers(path)), None)
        if mapping is None:
            continue
        patterns = mapping.test_globs(path)
        tested = any(
            other != path and fnmatch.fnmatch(other, pattern)
            for other in changed
            for pattern in patterns
        )
        if not tested:
            found.append(Uncovered(path, line, mapping))
    return found


def change_of(path: str, base: str, cwd: Path) -> str:
    text = _git(["diff", "--no-color", "--no-ext-diff", base, "--", path], cwd)
    if not text:
        try:
            text = (cwd / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
    return text[:MAX_DIFF_CHARS_PER_FILE]


def suggest_tests(changes: Mapping[str, str], env: Mapping[str, str]) -> dict[str, list[str]]:
    """Ask the platform's model gateway for test cases, returning none when it is unavailable.

    The gateway speaks the Anthropic Messages API, and every call it receives passes through the
    platform's guardrails and tracing. The Langfuse headers the platform sets group this call
    under the run in the trace view.
    """
    base_url, model = env.get("ANTHROPIC_BASE_URL", ""), env.get("AI_REVIEW_MODEL", "")
    if not changes or not base_url.startswith(("http://", "https://")) or not model:
        return {}
    api_key = env.get("ANTHROPIC_API_KEY", "")
    headers = {
        "content-type": "application/json",
        "anthropic-version": "2023-06-01",
        "authorization": "Bearer " + api_key,
        "x-api-key": api_key,
    }
    for line in env.get("ANTHROPIC_CUSTOM_HEADERS", "").splitlines():
        name, _, value = line.partition(":")
        if name.strip():
            headers[name.strip()] = value.strip()
    prompt = "\n\n".join(f"File: {path}\n```diff\n{diff}\n```" for path, diff in changes.items())
    body = {
        "model": model,
        "max_tokens": 2048,
        "temperature": 0,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        payload = _messages(f"{base_url.rstrip('/')}/v1/messages", headers, body)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        print(f"{TOOL}: no test suggestions, the gateway call failed: {exc}", file=sys.stderr)
        return {}
    text = "".join(
        block.get("text", "") for block in payload.get("content", []) if block.get("type") == "text"
    )
    return parse_suggestions(text, set(changes))


def _messages(url: str, headers: Mapping[str, str], body: dict) -> dict:
    try:
        return _post(url, headers, body)
    except urllib.error.HTTPError as exc:
        # Some models, such as Claude Opus 5.5 on Bedrock, refuse the temperature parameter.
        if exc.code == 400 and "temperature" in exc.read().decode("utf-8", "replace"):
            return _post(url, headers, {k: v for k, v in body.items() if k != "temperature"})
        raise


def _post(url: str, headers: Mapping[str, str], body: dict) -> dict:
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=dict(headers), method="POST"
    )
    with urllib.request.urlopen(request, timeout=MODEL_TIMEOUT_SECONDS) as response:
        return json.load(response)


def parse_suggestions(text: str, paths: set[str]) -> dict[str, list[str]]:
    start, end = text.find("{"), text.rfind("}")
    try:
        raw = json.loads(text[start : end + 1]) if start >= 0 else {}
    except json.JSONDecodeError:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        path: [str(item)[:300] for item in cases if isinstance(item, str)][:MAX_SUGGESTIONS]
        for path, cases in raw.items()
        if path in paths and isinstance(cases, list)
    }


def review(findings: list[Uncovered], suggestions: Mapping[str, list[str]]) -> dict:
    comments = []
    for finding in findings:
        detail = FIXED_DETAIL
        if suggestions.get(finding.path):
            cases = "\n".join(f"- {case}" for case in suggestions[finding.path])
            detail = f"{FIXED_DETAIL}\n\nSuggested test cases:\n{cases}"
        comments.append(
            {
                "path": finding.path,
                "line": finding.line,
                "severity": finding.mapping.severity,
                "title": f"{finding.path} changes without a {finding.mapping.name} test change",
                "detail": detail,
                "category": "test-coverage",
            }
        )
    action = "REQUEST_CHANGES" if comments else "APPROVE"
    summary = f"{len(comments)} changed source file(s) without a test change"
    return {"review_action": action, "summary": summary, "comments": comments}


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--against", required=True, help="Base ref of the change.")
    parser.add_argument("--json-out", required=True, help="File the review JSON is written to.")
    parser.add_argument(
        "--rules",
        default=os.environ.get("TEST_POLICY_RULES", Path(__file__).with_name("rules.toml")),
        help="TOML rules file.",
    )
    args = parser.parse_args(argv)
    env = os.environ if env is None else env
    try:
        mappings = load_rules(Path(args.rules))
    except RulesError as exc:
        print(f"{TOOL}: {exc}", file=sys.stderr)
        return 2
    cwd = Path.cwd()
    base = change_base(args.against, cwd)
    findings = uncovered(mappings, changed_files(base, cwd))
    changes = {finding.path: change_of(finding.path, base, cwd) for finding in findings}
    document = review(findings, suggest_tests(changes, env))
    Path(args.json_out).write_text(json.dumps(document, indent=2), encoding="utf-8")
    print(f"{TOOL}: {document['summary']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
