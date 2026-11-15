"""The exception review flags expired, expiring, and cleared entries without the network."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from scripts import exception_review
from scripts.upstream_releases import CLEAN, FINDINGS, UNAVAILABLE, Release, Scan
from services.integration.image_policy import FindingException
from tests.support import hermetic_settings

TODAY = date(2026, 11, 13)
OLD = "sha256:" + "1" * 64
NEW = "sha256:" + "2" * 64
UPSTREAM_DIGEST = "sha256:" + "3" * 64
ECR = "111111111111.dkr.ecr.us-east-1.amazonaws.com/agentic-platform"
REBUILT = f"{ECR}/trivy:0.3.0-abc-arm64@{NEW}"
MANIFEST = [
    {
        "name": "trivy",
        "repository": f"{ECR}/trivy",
        "digest": NEW,
        "reference": REBUILT,
        "pushed": True,
    }
]
TRIVY = Release(
    component="trivy",
    repository="aquasecurity/trivy",
    pin="TRIVY_VERSION",
    pinned="v0.76.2",
    prefix="TRIVY",
    image_mode="build",
    upstream_image=f"docker.io/aquasec/trivy:0.76.2@{UPSTREAM_DIGEST}",
    scan_image="docker.io/aquasec/trivy:0.77.0",
)
POLICY = f"""config_version: 1
platform_registries: [111111111111.dkr.ecr.us-east-1.amazonaws.com]
catalog: [docker.io/aquasec/trivy]
tenant_prefix: {ECR}/tenants/{{tenant}}/
"""


def exception(
    image: str = f"{ECR}/trivy@{OLD}",
    finding: str = "CVE-2026-1",
    expires: date = date(2027, 1, 31),
) -> FindingException:
    return FindingException(
        image=image, finding=finding, reason="Vendor fix pending.", expires=expires
    )


def run_review(entries, scans):
    return exception_review.review(entries, TODAY, 14, MANIFEST, [TRIVY], scans)


def test_an_ecr_exception_is_rechecked_on_the_rebuild_and_the_newest_upstream_image() -> None:
    assert exception_review.targets_for(exception(), MANIFEST, [TRIVY]) == [
        exception_review.Target(exception_review.REBUILD, REBUILT),
        exception_review.Target(exception_review.UPSTREAM, "docker.io/aquasec/trivy:0.77.0"),
    ]


def test_an_upstream_exception_matches_its_component_by_the_upstream_image_name() -> None:
    upstream = exception(image=f"docker.io/aquasec/trivy@{UPSTREAM_DIGEST}")

    assert exception_review.targets_for(upstream, MANIFEST, [TRIVY]) == [
        exception_review.Target(exception_review.EXCEPTED, upstream.image),
        exception_review.Target(exception_review.UPSTREAM, "docker.io/aquasec/trivy:0.77.0"),
    ]


def test_an_image_no_component_builds_is_rescanned_at_its_own_digest() -> None:
    python = exception(image=f"docker.io/library/python@{OLD}")

    assert exception_review.targets_for(python, MANIFEST, [TRIVY]) == [
        exception_review.Target(exception_review.EXCEPTED, python.image)
    ]


def test_a_finding_the_rebuild_no_longer_reports_clears_the_exception() -> None:
    scans = {
        REBUILT: Scan(REBUILT, CLEAN),
        "docker.io/aquasec/trivy:0.77.0": Scan("x", FINDINGS, ("CVE-2026-1",)),
    }

    [entry] = run_review([exception()], scans)

    assert entry.flags == (exception_review.CLEARED,)
    assert entry.describe() == f"The rebuild `{REBUILT}` no longer reports it. Remove the entry."
    assert entry.scans() == "Rebuild does not report it, newest upstream image reports it."


def test_an_expired_and_an_expiring_exception_are_flagged() -> None:
    expired = exception(expires=date(2026, 11, 12))
    expiring = exception(finding="CVE-2026-2", expires=date(2026, 11, 27))
    later = exception(finding="CVE-2026-3", expires=date(2026, 11, 28))
    scans = {REBUILT: Scan(REBUILT, FINDINGS, ("CVE-2026-1", "CVE-2026-2", "CVE-2026-3"))}

    reviews = run_review([expired, expiring, later], scans)

    assert [r.flags for r in reviews] == [
        (exception_review.EXPIRED,),
        (exception_review.EXPIRING,),
        (),
    ]
    assert reviews[0].describe() == (
        "Expired on 2026-11-12, so the scan gate refuses the finding again. "
        "Every completed scan still reports it."
    )
    assert reviews[1].describe() == "Expires in 14 day(s). Every completed scan still reports it."


def test_an_exception_no_scan_completed_for_is_reported_as_not_rechecked() -> None:
    scans = {REBUILT: Scan(REBUILT, UNAVAILABLE)}

    [entry] = run_review([exception()], scans)

    assert entry.flags == ()
    assert entry.describe() == "No scan completed, so the finding was not re-checked."
    assert entry.scans() == "Rebuild not scanned, newest upstream image not scanned."


def test_the_report_counts_each_flag() -> None:
    reviews = run_review(
        [exception(expires=date(2026, 11, 1)), exception(finding="CVE-2026-9")],
        {REBUILT: Scan(REBUILT, FINDINGS, ("CVE-2026-1",))},
    )
    loaded = exception_review.Loaded(exception_review.PLATFORM, "x.yaml", version="v1")

    text = exception_review.section(loaded, reviews, TODAY, 14)

    assert text.startswith("### Platform exceptions")
    assert (
        "The file is `x.yaml` at version `v1`. 2 exception(s) reviewed on 2026-11-13, 1 expired, "
        "0 expiring within 14 days, and 1 whose finding a completed scan no longer reports."
    ) in text
    assert text.index("CVE-2026-1") < text.index("CVE-2026-9")


def _today() -> date:
    return datetime.now(UTC).date()


def entry(finding: str, *, created: int = -5, expires: int = 20, image: str = "") -> dict:
    today = _today()
    return {
        "image": image or f"{ECR}/trivy@{OLD}",
        "finding": finding,
        "reason": "Vendor fix pending.",
        "created": (today + timedelta(days=created)).isoformat(),
        "expires": (today + timedelta(days=expires)).isoformat(),
        "requester": "alice",
        "approver": "bob",
    }


def write(path: Path, entries: list[dict], header: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump({"config_version": 1, "exceptions": entries}, sort_keys=False)
    path.write_text(header + body, encoding="utf-8")


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "env"
    directory.mkdir()
    (directory / "image_policy.yaml").write_text(POLICY, encoding="utf-8")
    monkeypatch.setenv("PLATFORM_ENV_CONFIG_DIR", str(directory))
    settings = hermetic_settings(environment="test", tenant_config_bucket="")
    monkeypatch.setattr(exception_review, "_settings", lambda: settings)
    return directory


def report(tmp_path: Path, *extra: str) -> list[str]:
    none = str(tmp_path / "none.json")
    return [
        "report",
        "--manifest",
        none,
        "--releases",
        none,
        "--scans",
        str(tmp_path / "none.jsonl"),
        "--output",
        str(tmp_path / "review.md"),
        "--warnings",
        str(tmp_path / "warnings.md"),
        *extra,
    ]


def test_a_missing_platform_file_fails_the_report_and_opens_it_with_a_warning(
    env: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert exception_review.main(report(tmp_path)) == 1

    warning = (tmp_path / "warnings.md").read_text()
    assert warning.startswith("> [!WARNING]")
    assert f"{env / 'image_exceptions.yaml'} is missing" in warning
    assert "::error title=Finding exception review::" in capsys.readouterr().err
    assert "could not be reviewed" in (tmp_path / "review.md").read_text()


def test_a_malformed_platform_file_fails_the_report(env: Path, tmp_path: Path) -> None:
    (env / "image_exceptions.yaml").write_text(
        "exceptions:\n  - image: docker.io/library/python:3\n", encoding="utf-8"
    )

    assert exception_review.main(report(tmp_path)) == 1
    assert "config_version must be 1" in (tmp_path / "warnings.md").read_text()


def test_an_empty_platform_file_passes_with_no_warning(env: Path, tmp_path: Path) -> None:
    write(env / "image_exceptions.yaml", [])

    assert exception_review.main(report(tmp_path)) == 0
    assert (tmp_path / "warnings.md").read_text() == ""
    assert "It holds no exception." in (tmp_path / "review.md").read_text()


def test_prune_removes_expired_and_resolved_platform_entries_and_no_tenant_entry(
    env: Path, tmp_path: Path
) -> None:
    platform = env / "image_exceptions.yaml"
    write(
        platform,
        [
            entry("CVE-2026-1", created=-20, expires=-1),
            entry("CVE-2026-2"),
            entry("CVE-2026-3"),
        ],
        header="# Platform exceptions.\n",
    )
    tenant = env / "tenants" / "adp" / "image_exceptions.yaml"
    write(
        tenant, [entry("CVE-2026-4", created=-20, expires=-1, image=f"{ECR}/tenants/adp/a@{OLD}")]
    )
    tenant_text = tenant.read_text()
    (tmp_path / "manifest.json").write_text(json.dumps(MANIFEST), encoding="utf-8")
    log = tmp_path / "rebuild.log"
    log.write_text("[image-scan]   CVE-2026-3 (high) /usr/bin/x 1 fixed in 2\n", encoding="utf-8")
    scan = {"image": REBUILT, "pulled": True, "exit_code": 1, "resolved": "", "log": str(log)}
    (tmp_path / "scans.jsonl").write_text(json.dumps(scan) + "\n", encoding="utf-8")
    argv = report(tmp_path, "--prune")
    argv[argv.index("--manifest") + 1] = str(tmp_path / "manifest.json")
    argv[argv.index("--scans") + 1] = str(tmp_path / "scans.jsonl")

    assert exception_review.main(argv) == 0

    remaining = yaml.safe_load(platform.read_text())["exceptions"]
    assert [e["finding"] for e in remaining] == ["CVE-2026-3"]
    assert platform.read_text().startswith("# Platform exceptions.\n")
    assert tenant.read_text() == tenant_text
    text = (tmp_path / "review.md").read_text()
    assert f"Removed from `{platform}`, now at version `" in text
    assert "- CVE-2026-1 in" in text
    assert (
        f"- CVE-2026-2 in `{ECR}/trivy@{OLD}`, the rebuild `{REBUILT}` no longer reports it" in text
    )
    assert "### Tenant adp exceptions" in text
    assert "The review removes no tenant entry." in text


def test_targets_lists_each_image_once(env: Path, tmp_path: Path) -> None:
    exceptions = tmp_path / "image_exceptions.yaml"
    write(exceptions, [entry("CVE-2026-1"), entry("CVE-2026-2")])
    manifest = tmp_path / "images-manifest.json"
    manifest.write_text(json.dumps(MANIFEST), encoding="utf-8")
    releases = tmp_path / "releases.json"
    releases.write_text(json.dumps([TRIVY.__dict__]), encoding="utf-8")
    output = tmp_path / "targets.json"

    exception_review.main(
        [
            "targets",
            "--exceptions",
            str(exceptions),
            "--manifest",
            str(manifest),
            "--releases",
            str(releases),
            "--output",
            str(output),
        ]
    )

    assert [t["image"] for t in json.loads(output.read_text())] == [
        REBUILT,
        "docker.io/aquasec/trivy:0.77.0",
    ]
