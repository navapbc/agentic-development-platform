"""Unit tests for the scan that runs before an image is pushed."""

from __future__ import annotations

import json
import subprocess
from datetime import date
from pathlib import Path

from scripts import image_scan
from scripts.image_scan import Finding, ImageScanner
from services.integration.image_policy import FindingException

DIGEST = "sha256:" + "a" * 64
IMAGE = f"123456789012.dkr.ecr.us-east-1.amazonaws.com/agentic-platform/semgrep:1@{DIGEST}"


class _Client:
    def __init__(self, vulnerabilities: list[dict]) -> None:
        self.vulnerabilities = vulnerabilities
        self.sboms: list[dict] = []

    def scan_sbom(self, sbom, outputFormat):  # noqa: N803 - the boto3 parameter name
        self.sboms.append(sbom)
        return {"sbom": {"vulnerabilities": self.vulnerabilities}}


class _Session:
    def __init__(self, client: _Client) -> None:
        self._client = client

    def client(self, name: str):
        assert name == "inspector-scan"
        return self._client


def _runner(commands: list[list[str]]):
    def run(command, **_):
        commands.append(command)
        if command[:2] == ["docker", "run"]:
            work = Path(command[command.index("--volume") + 1].split(":", 1)[0])
            (work / "sbom.json").write_text(json.dumps({"components": [1]}))
        return subprocess.CompletedProcess(command, 0, "", "")

    return run


def _scanner(vulnerabilities, exceptions=(), commands=None) -> ImageScanner:
    scanner = ImageScanner(
        _Session(_Client(vulnerabilities)),
        "arm64",
        exceptions=exceptions,
        run=_runner(commands if commands is not None else []),
    )
    scanner._scanner = "scanner:1"
    return scanner


def test_a_high_or_critical_finding_blocks_and_a_medium_one_does_not() -> None:
    result = _scanner(
        [
            {"id": "CVE-1", "severity": "critical"},
            {"id": "CVE-2", "severity": "high"},
            {"id": "CVE-3", "severity": "medium"},
        ]
    ).scan(IMAGE, [IMAGE])

    assert not result.passed
    assert result.blocking == (Finding("CVE-1", "critical"), Finding("CVE-2", "high"))
    assert "2 blocking finding(s)" in result.describe()


def test_an_exception_for_the_digest_accepts_its_finding() -> None:
    exception = FindingException(IMAGE.replace(":1@", "@"), "CVE-2", "reason", date(2099, 1, 1))

    result = _scanner([{"id": "CVE-2", "severity": "high"}], [exception]).scan(IMAGE, [IMAGE])

    assert result.passed
    assert result.accepted == (Finding("CVE-2", "high"),)


def test_the_image_is_saved_for_its_platform_and_inventoried_in_the_scanner_image() -> None:
    commands: list[list[str]] = []

    _scanner([], commands=commands).scan(IMAGE, [IMAGE])

    assert commands[0][:4] == ["docker", "save", "--platform", "linux/arm64"]
    assert commands[0][-1] == IMAGE
    assert commands[1][commands[1].index("scanner:1") + 1 :][:3] == [
        "container",
        "--image",
        "/work/image.tar",
    ]


def test_a_refused_image_is_not_pushed_and_is_reported(monkeypatch) -> None:
    from scripts import publish_platform_images as publisher

    class _Ecr:
        def describe_images(self, **_):
            raise AssertionError("not reached")

    commands: list[list[str]] = []
    monkeypatch.setattr(publisher, "_pushed_digest", lambda ecr, repository, tag: "")
    monkeypatch.setattr(publisher, "_docker_login", lambda ecr, registry: None)
    scanner = _scanner([{"id": "CVE-9", "severity": "high"}])

    def run(command, **kwargs):
        commands.append(command)
        if command[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(command, 0, DIGEST + "\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    pub = publisher.Publisher(_Ecr(), "r.example", "agentic-platform", "arm64", scanner, run)
    reference = pub.built(publisher.BUILDAH, image_scan.REPO_ROOT, "0.3.0")

    assert reference.endswith("@refused")
    assert pub.refused and "CVE-9" in pub.refused[0]
    assert not any(command[:2] == ["docker", "push"] for command in commands)
    assert any("--load" in command for command in commands)
