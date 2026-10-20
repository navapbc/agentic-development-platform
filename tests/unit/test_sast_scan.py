"""Tests for the SAST gate's report parsing and register matching."""

from __future__ import annotations

from pathlib import Path

import pytest
from scripts import sast_scan
from scripts.sast_scan import Accepted, Finding


def _finding(**overrides: object) -> Finding:
    values: dict = {
        "tool": "bandit",
        "rule": "B105",
        "path": "pkg/mod.py",
        "line": 3,
        "match": 'PASS = "pass"',
        "severity": "LOW/MEDIUM",
        "message": "Possible hardcoded password",
    }
    values.update(overrides)
    return Finding(**values)


def _accepted(**overrides: object) -> Accepted:
    values: dict = {
        "tool": "bandit",
        "rule": "B105",
        "path": "pkg/mod.py",
        "match": 'PASS = "pass"',
        "justification": "An outcome name, not a credential.",
    }
    values.update(overrides)
    return Accepted(**values)


def test_a_registered_finding_passes_even_after_its_line_moves():
    result = sast_scan.evaluate([_finding(line=40)], [_accepted()])

    assert result.ok


def test_an_unregistered_finding_fails_the_gate():
    result = sast_scan.evaluate([_finding(rule="B603")], [])

    assert not result.ok
    assert [finding.rule for finding in result.unaccepted] == ["B603"]


def test_a_changed_line_is_no_longer_accepted():
    result = sast_scan.evaluate([_finding(match='PASS = "passed"')], [_accepted()])

    assert result.unaccepted
    assert result.stale


def test_a_register_entry_without_a_finding_is_stale():
    result = sast_scan.evaluate([], [_accepted()])

    assert not result.ok
    assert result.stale == [_accepted()]


def test_load_register_rejects_an_entry_without_a_justification(tmp_path: Path):
    register = tmp_path / "register.yaml"
    register.write_text(
        "- tool: bandit\n  rule: B105\n  path: a.py\n  match: x\n  justification: ' '\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="empty justification"):
        sast_scan.load_register(register)


def test_load_register_rejects_an_entry_missing_a_field(tmp_path: Path):
    register = tmp_path / "register.yaml"
    register.write_text("- tool: bandit\n  rule: B105\n", encoding="utf-8")

    with pytest.raises(ValueError, match="missing"):
        sast_scan.load_register(register)


def test_the_committed_register_loads_and_justifies_every_entry():
    entries = sast_scan.load_register()

    assert entries
    assert all(entry.justification for entry in entries)


def test_parse_bandit_reads_the_source_line_from_the_staged_tree(tmp_path: Path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text('import os\n\nPASS =   "pass"\n', encoding="utf-8")
    payload = {
        "results": [
            {
                "filename": str(tmp_path / "pkg" / "mod.py"),
                "line_number": 3,
                "test_id": "B105",
                "issue_severity": "LOW",
                "issue_confidence": "MEDIUM",
                "issue_text": "Possible hardcoded password: 'pass'",
            }
        ]
    }

    [finding] = sast_scan.parse_bandit(payload, tmp_path)

    assert finding.path == "pkg/mod.py"
    assert finding.match == 'PASS = "pass"'
    assert finding.severity == "LOW/MEDIUM"


def test_parse_semgrep_maps_container_paths_back_to_the_tree(tmp_path: Path):
    (tmp_path / "ci.yml").write_text("steps:\n  - uses: actions/checkout@v7\n", encoding="utf-8")
    payload = {
        "results": [
            {
                "check_id": "yaml.github-actions.mutable-action-tag",
                "path": "/src/ci.yml",
                "start": {"line": 2},
                "extra": {"severity": "WARNING", "message": "Pin the action to a commit SHA."},
            }
        ]
    }

    [finding] = sast_scan.parse_semgrep(payload, tmp_path)

    assert finding.path == "ci.yml"
    assert finding.match == "- uses: actions/checkout@v7"


def test_parse_trivy_keeps_only_failed_misconfigurations(tmp_path: Path):
    payload = {
        "Results": [
            {
                "Target": "docker/Dockerfile",
                "Misconfigurations": [
                    {
                        "ID": "DS-0026",
                        "Status": "FAIL",
                        "Severity": "LOW",
                        "Title": "No HEALTHCHECK",
                    },
                    {"ID": "DS-0002", "Status": "PASS", "Severity": "HIGH", "Title": "Root user"},
                ],
            }
        ]
    }

    findings = sast_scan.parse_trivy(payload, tmp_path)

    assert [(finding.rule, finding.line, finding.match) for finding in findings] == [
        ("DS-0026", 0, "")
    ]


def test_parse_gitleaks_never_records_the_matched_secret(tmp_path: Path):
    (tmp_path / "t.py").write_text("token = 'value-that-looks-secret'\n", encoding="utf-8")
    leak = {"File": "/src/t.py", "StartLine": 1, "RuleID": "generic-api-key", "Commit": "abc"}

    [tree] = sast_scan.parse_gitleaks([leak], tmp_path, history=False)
    [history] = sast_scan.parse_gitleaks([{**leak, "File": "t.py"}], tmp_path, history=True)

    assert tree.match.startswith("sha256:")
    assert "value-that-looks-secret" not in tree.match
    assert history.tool == "gitleaks-history"
    assert history.match == "abc:t.py:generic-api-key:1"


def test_parse_pip_audit_reports_each_vulnerability():
    payload = {
        "dependencies": [
            {
                "name": "urllib3",
                "version": "2.7.0",
                "vulns": [{"id": "GHSA-1", "fix_versions": ["2.8.0"]}],
            },
            {"name": "httpx", "version": "0.28.1", "vulns": []},
        ]
    }

    [finding] = sast_scan.parse_pip_audit(payload)

    assert (finding.path, finding.rule, finding.match) == ("urllib3", "GHSA-1", "urllib3==2.7.0")
    assert finding.message == "fixed in 2.8.0"
