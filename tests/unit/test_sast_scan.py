"""Tests for the SAST gate's report parsing and register matching."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import httpx
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


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(cwd: Path, message: str) -> None:
    _git(
        cwd,
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        message,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = (tmp_path / "repo").resolve()
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(
        root,
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "a",
    )
    return root


def test_head_commit_ignores_refs_outside_the_checked_out_history(repo: Path):
    head = _git(repo, "rev-parse", "HEAD")
    _git(
        repo,
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "b",
    )
    _git(repo, "update-ref", "refs/agents/checkpoint", "HEAD")
    _git(repo, "reset", "-q", "--hard", head)

    assert sast_scan.head_commit(repo) == head


def test_history_mounts_add_the_shared_git_directory_for_a_linked_worktree(repo: Path):
    assert sast_scan.history_mounts(repo) == [f"{repo}:{repo}:ro"]

    linked = repo.parent / "linked"
    _git(repo, "worktree", "add", "-q", "--detach", str(linked))
    common = repo / ".git"

    assert sast_scan.history_mounts(linked) == [f"{linked}:{linked}:ro", f"{common}:{common}:ro"]


_NOW = datetime(2026, 10, 22, tzinfo=UTC)


def _locked(name: str, uploaded: str, *, registry: bool = True) -> dict:
    source = {"registry": "https://pypi.org/simple"} if registry else {"editable": "."}
    return {
        "name": name,
        "version": "1.0",
        "source": source,
        "sdist": {"url": f"https://files.example/{name}-1.0.tar.gz", "upload-time": uploaded},
        "wheels": [
            {
                "url": f"https://files.example/{name}-1.0-py3-none-any.whl",
                "upload-time": "2026-10-21T00:00:00Z",
            }
        ],
    }


def test_fresh_releases_lists_only_registry_releases_inside_the_window():
    lock = {
        "package": [
            _locked("young", "2026-10-20T00:00:00Z"),
            _locked("old", "2026-10-01T00:00:00Z"),
            _locked("local", "2026-10-21T00:00:00Z", registry=False),
        ]
    }

    fresh = sast_scan.fresh_releases(lock, _NOW)

    assert fresh == [("young", "1.0", "2026-10-20T00:00:00Z", "young-1.0.tar.gz")]


def test_fresh_releases_dates_a_release_by_its_first_uploaded_file():
    lock = {"package": [_locked("backfilled", "2026-09-01T00:00:00Z")]}

    assert sast_scan.fresh_releases(lock, _NOW) == []


def test_fresh_release_report_never_fails_and_records_attestation(tmp_path: Path):
    lock = tmp_path / "uv.lock"
    lock.write_text(
        'version = 1\n\n[[package]]\nname = "young"\nversion = "1.0"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
        'sdist = { url = "https://files.example/young-1.0.tar.gz", '
        'upload-time = "2026-10-20T00:00:00Z" }\n',
        encoding="utf-8",
    )
    calls: list[tuple[str, str, str]] = []

    def attestation(name: str, version: str, filename: str) -> str:
        calls.append((name, version, filename))
        return "unattested"

    [release] = sast_scan.fresh_release_report(lock, now=_NOW, attestation=attestation)

    assert calls == [("young", "1.0", "young-1.0.tar.gz")]
    assert (release.name, release.age_days, release.attestation) == ("young", 2.0, "unattested")


def test_pypi_attestation_reads_the_publisher():
    def handler(request: httpx.Request) -> httpx.Response:
        if "unsigned" in request.url.path:
            return httpx.Response(404)
        bundle = {"publisher": {"repository": "org/project", "workflow": "release.yml"}}
        return httpx.Response(200, json={"attestation_bundles": [bundle]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        signed = sast_scan.pypi_attestation(client, "signed", "1.0", "signed-1.0.tar.gz")
        unsigned = sast_scan.pypi_attestation(client, "unsigned", "1.0", "unsigned-1.0.tar.gz")

    assert signed == "attested by org/project release.yml"
    assert unsigned == "unattested"
