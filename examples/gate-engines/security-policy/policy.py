"""Apply an organization's security rules to the lines a change adds, and write SARIF 2.1.0.

The platform runs `bin/scan --against <ref> --json-out <file>` in the checkout. Each rule in the
TOML rules file is a regular expression matched against every line the change adds to a file its
path patterns select. The exit status is 0 with or without findings and 2 for a rules file the
engine cannot load.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

TOOL = "security-policy"
VERSION = "1.0.0"
GIT_TIMEOUT_SECONDS = 120
MAX_UNTRACKED_BYTES = 1_000_000
SEVERITY = {
    "critical": ("error", "9.5"),
    "high": ("error", "8.0"),
    "medium": ("warning", "5.5"),
    "low": ("note", "2.0"),
}
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


class RulesError(ValueError):
    """The rules file is missing, malformed, or names an unknown severity."""


@dataclass(frozen=True)
class Rule:
    id: str
    description: str
    pattern: re.Pattern[str]
    severity: str
    paths: tuple[str, ...]
    exclude: tuple[str, ...]

    def selects(self, path: str) -> bool:
        return any(fnmatch.fnmatch(path, p) for p in self.paths) and not any(
            fnmatch.fnmatch(path, p) for p in self.exclude
        )


@dataclass(frozen=True)
class Finding:
    rule: Rule
    path: str
    line: int


def load_rules(path: Path) -> list[Rule]:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RulesError(f"cannot read the rules file {path}: {exc}") from exc
    rules = []
    for entry in raw.get("rule", []):
        severity = str(entry.get("severity", "medium")).lower()
        if severity not in SEVERITY:
            raise RulesError(f"rule {entry.get('id')!r} has unknown severity {severity!r}")
        try:
            pattern = re.compile(entry["pattern"])
            rule = Rule(
                id=entry["id"],
                description=entry["description"],
                pattern=pattern,
                severity=severity,
                paths=tuple(entry.get("paths", ["*"])),
                exclude=tuple(entry.get("exclude", [])),
            )
        except (KeyError, re.error) as exc:
            raise RulesError(f"rule {entry.get('id')!r} is invalid: {exc}") from exc
        rules.append(rule)
    return rules


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


def added_lines(against: str, cwd: Path) -> dict[str, list[tuple[int, str]]]:
    """Return each file's added lines with their line numbers, untracked files included."""
    added: dict[str, list[tuple[int, str]]] = {}
    path, line = "", 0
    diff = _git(
        ["diff", "--unified=0", "--no-color", "--no-ext-diff", change_base(against, cwd)], cwd
    )
    for text in diff.splitlines():
        if text.startswith("+++ "):
            path = text[6:] if text.startswith("+++ b/") else ""
        elif hunk := _HUNK.match(text):
            line = int(hunk.group(1))
        elif text.startswith("+") and path:
            added.setdefault(path, []).append((line, text[1:]))
            line += 1
    untracked = _git(["ls-files", "--others", "--exclude-standard", "-z"], cwd)
    for name in filter(None, untracked.split("\0")):
        content = _text_of(cwd / name)
        if content is not None:
            added[name] = list(enumerate(content.splitlines(), start=1))
    return added


def _text_of(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_UNTRACKED_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    return None if b"\0" in data[:8192] else data.decode("utf-8", "replace")


def scan(rules: list[Rule], added: dict[str, list[tuple[int, str]]]) -> list[Finding]:
    findings = []
    for path, lines in sorted(added.items()):
        for rule in rules:
            if not rule.selects(path):
                continue
            findings.extend(
                Finding(rule, path, n) for n, text in lines if rule.pattern.search(text)
            )
    return findings


def sarif(rules: list[Rule], findings: list[Finding]) -> dict:
    """Build the SARIF document. A result names its rule and location, never the matched text,
    so a committed credential is not copied into the gate evidence."""
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": TOOL,
                        "version": VERSION,
                        "rules": [
                            {
                                "id": rule.id,
                                "shortDescription": {"text": rule.description},
                                "defaultConfiguration": {"level": SEVERITY[rule.severity][0]},
                                "properties": {"security-severity": SEVERITY[rule.severity][1]},
                            }
                            for rule in rules
                        ],
                    }
                },
                "results": [
                    {
                        "ruleId": finding.rule.id,
                        "level": SEVERITY[finding.rule.severity][0],
                        "message": {"text": finding.rule.description},
                        "locations": [
                            {
                                "physicalLocation": {
                                    "artifactLocation": {"uri": finding.path},
                                    "region": {"startLine": finding.line},
                                }
                            }
                        ],
                    }
                    for finding in findings
                ],
            }
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--against", required=True, help="Base ref of the change.")
    parser.add_argument("--json-out", required=True, help="File the SARIF document is written to.")
    parser.add_argument(
        "--rules",
        default=os.environ.get("SECURITY_POLICY_RULES", Path(__file__).with_name("rules.toml")),
        help="TOML rules file.",
    )
    args = parser.parse_args(argv)
    try:
        rules = load_rules(Path(args.rules))
    except RulesError as exc:
        print(f"{TOOL}: {exc}", file=sys.stderr)
        return 2
    findings = scan(rules, added_lines(args.against, Path.cwd()))
    Path(args.json_out).write_text(json.dumps(sarif(rules, findings), indent=2), encoding="utf-8")
    print(f"{TOOL}: {len(findings)} finding(s) from {len(rules)} rule(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
