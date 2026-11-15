"""Unit tests for the image policy, the admission before each pull, and the in-pod rules."""

from __future__ import annotations

import json
from datetime import date

import pytest
from services.integration.credentials import ConfigError
from services.integration.image_policy import (
    CACHE,
    CATALOG,
    REGISTRY,
    TENANT,
    FindingException,
    ImageReference,
    admit,
    parse_exceptions,
    parse_policy,
    parse_reference,
)
from services.orchestration.gates import image_admission
from services.orchestration.gates.image_admission import (
    Admission,
    ImageRefusedError,
    ImageScanPendingError,
    containers_policy,
    require_clean_scan,
    trust_policy,
    verify_script,
)

HOST = "123456789012.dkr.ecr.us-east-1.amazonaws.com"
DIGEST = "sha256:" + "a" * 64
CHILD = "sha256:" + "b" * 64
POLICY = parse_policy(
    {
        "platform_registries": [HOST],
        "catalog": [f"{HOST}/agentic-platform/semgrep"],
        "tenant_prefix": f"{HOST}/agentic-platform/tenants/{{tenant}}/",
        "caches": [f"{HOST}/agentic-platform-ecr-public/"],
        "scan": {"gate": "inspector", "severity": "HIGH"},
        "signatures": {
            "verification": True,
            "platform_profile": "arn:aws:signer:us-east-1:123456789012:/signing-profiles/platform",
            "tenant_profiles": {
                "adp": "arn:aws:signer:us-east-1:123456789012:/signing-profiles/t_adp"
            },
        },
    }
)
MIRRORS = {"docker.io/library": f"{HOST}/agentic-platform-ecr-public/docker/library"}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("python:3.13@" + DIGEST, ImageReference("docker.io", "library/python", "3.13", DIGEST)),
        ("aquasec/trivy:0.74.0", ImageReference("docker.io", "aquasec/trivy", "0.74.0")),
        ("localhost:5001/engine:1", ImageReference("localhost:5001", "engine", "1")),
        (f"{HOST}/a/b@{DIGEST}", ImageReference(HOST, "a/b", "", DIGEST)),
    ],
)
def test_a_reference_names_its_registry_the_way_the_pull_does(text, expected) -> None:
    assert parse_reference(text) == expected


def test_each_kind_of_image_is_admitted_from_its_own_entry() -> None:
    assert admit(f"{HOST}/agentic-platform/semgrep:1@{DIGEST}", POLICY, "adp").kind == CATALOG
    assert admit(f"{HOST}/agentic-platform/tenants/adp/x@{DIGEST}", POLICY, "adp").kind == TENANT
    cached = admit(f"python:3.13@{DIGEST}", POLICY, "adp", MIRRORS)
    assert cached.kind == CACHE
    assert cached.resolved.name == f"{HOST}/agentic-platform-ecr-public/docker/library/python"


@pytest.mark.parametrize(
    ("image", "reason"),
    [
        (f"{HOST}/agentic-platform/semgrep:1", "carries no @sha256: digest"),
        (f"{HOST}/agentic-platform/tenants/other/x@{DIGEST}", "does not admit for tenant adp"),
        (f"{HOST}/agentic-platform/semgrep-fork@{DIGEST}", "does not admit"),
        (f"quay.io/buildah/stable@{DIGEST}", "does not admit"),
    ],
)
def test_the_policy_refuses_an_image_it_does_not_admit(image, reason) -> None:
    with pytest.raises(ConfigError, match=reason):
        admit(image, POLICY, "adp", MIRRORS)


def test_a_tenant_mirror_may_not_point_into_another_tenants_prefix() -> None:
    external = {"ghcr.io": "artifactory.example.gov/ghcr-remote"}
    hijack = {"ghcr.io": f"{HOST}/agentic-platform/tenants/other"}

    assert admit(f"ghcr.io/a/b@{DIGEST}", POLICY, "adp", external).kind == REGISTRY
    with pytest.raises(ConfigError, match="does not admit"):
        admit(f"ghcr.io/a/b@{DIGEST}", POLICY, "adp", hijack)


def test_an_exception_needs_a_digest_and_an_expiry() -> None:
    with pytest.raises(ConfigError, match="needs reason, created, expires"):
        parse_exceptions(
            {
                "config_version": 1,
                "exceptions": [{"image": f"python@{DIGEST}", "finding": "CVE-1"}],
            },
            scope="tenant",
        )
    with pytest.raises(ConfigError, match="must carry an @sha256: digest"):
        parse_exceptions(
            {
                "config_version": 1,
                "exceptions": [
                    {
                        "image": "python:3",
                        "finding": "CVE-1",
                        "reason": "r",
                        "created": "2026-12-15",
                        "expires": "2027-01-01",
                    }
                ],
            },
            scope="tenant",
        )


class _RepositoryNotFound(Exception):
    pass


class _Ecr:
    class exceptions:  # noqa: N801 - the boto3 client attribute name
        RepositoryNotFoundException = _RepositoryNotFound

    def __init__(self, images: list[dict] | None) -> None:
        self.images = images

    def batch_get_image(self, **kwargs):
        if self.images is None:
            raise _RepositoryNotFound()
        return {"images": self.images}


class _Inspector:
    def __init__(self, status: dict | None, findings: list[str]) -> None:
        self.status = status
        self.findings = findings
        self.criteria: list[dict] = []

    def list_coverage(self, filterCriteria):  # noqa: N803 - the boto3 parameter name
        self.criteria.append(filterCriteria)
        return {"coveredResources": [{"scanStatus": self.status}] if self.status else []}

    def list_findings(self, filterCriteria, maxResults, nextToken=None):  # noqa: N803
        self.criteria.append(filterCriteria)
        return {
            "findings": [
                {"packageVulnerabilityDetails": {"vulnerabilityId": finding}}
                for finding in self.findings
            ]
        }


SCANNED = {"statusCode": "ACTIVE", "reason": "SUCCESSFUL"}
INDEX = {
    "imageManifestMediaType": "application/vnd.oci.image.index.v1+json",
    "imageManifest": json.dumps(
        {"manifests": [{"digest": CHILD, "platform": {"os": "linux", "architecture": "arm64"}}]}
    ),
}


def _scan(image, inspector, exceptions=(), ecr_images=(INDEX,)):
    clients = {
        "ecr": _Ecr(None if ecr_images is None else list(ecr_images)),
        "inspector2": inspector,
    }
    require_clean_scan(
        image, POLICY, list(exceptions), clients=clients, today=date(2026, 11, 9), arch="arm64"
    )


def test_a_scanned_image_without_blocking_findings_is_admitted_on_its_platform_image() -> None:
    inspector = _Inspector(SCANNED, [])

    _scan(admit(f"python:3.13@{DIGEST}", POLICY, "adp", MIRRORS), inspector)

    resource = inspector.criteria[0]["resourceId"][0]["value"]
    assert resource == (
        "arn:aws:ecr:us-east-1:123456789012:repository/"
        f"agentic-platform-ecr-public/docker/library/python/{CHILD}"
    )
    assert [s["value"] for s in inspector.criteria[1]["severity"]] == ["HIGH", "CRITICAL"]


def test_an_unscanned_image_waits_and_a_blocking_finding_refuses() -> None:
    image = admit(f"python:3.13@{DIGEST}", POLICY, "adp", MIRRORS)

    with pytest.raises(ImageScanPendingError, match="PENDING_INITIAL_SCAN"):
        _scan(image, _Inspector({"statusCode": "ACTIVE", "reason": "PENDING_INITIAL_SCAN"}, []))
    with pytest.raises(ImageRefusedError, match="CVE-2025-1"):
        _scan(image, _Inspector(SCANNED, ["CVE-2025-1"]))


def test_an_unexpired_exception_accepts_its_finding_and_an_expired_one_does_not() -> None:
    image = admit(f"python:3.13@{DIGEST}", POLICY, "adp", MIRRORS)
    accepted = FindingException(
        "docker.io/library/python@" + DIGEST, "CVE-1", "r", date(2027, 1, 1)
    )
    expired = FindingException("docker.io/library/python@" + DIGEST, "CVE-1", "r", date(2026, 1, 1))

    _scan(image, _Inspector(SCANNED, ["CVE-1"]), [accepted])
    with pytest.raises(ImageRefusedError, match="CVE-1"):
        _scan(image, _Inspector(SCANNED, ["CVE-1"]), [expired])


def test_a_catalog_image_missing_from_ecr_is_refused(monkeypatch) -> None:
    image = admit(f"{HOST}/agentic-platform/semgrep:1@{DIGEST}", POLICY, "adp")

    with pytest.raises(ImageRefusedError, match="is not in ECR"):
        _scan(image, _Inspector(SCANNED, []), ecr_images=[])


def test_a_cache_image_missing_from_ecr_is_pulled_into_the_cache_and_waits(monkeypatch) -> None:
    from services.orchestration.gates import registry_auth

    filled = []
    monkeypatch.setattr(registry_auth, "ecr_token", lambda account, region: "token")
    monkeypatch.setattr(image_admission, "_fill_cache", lambda *args: filled.append(args))
    image = admit(f"python:3.13@{DIGEST}", POLICY, "adp", MIRRORS)

    with pytest.raises(ImageScanPendingError, match="pulled into the pull-through cache"):
        _scan(image, _Inspector(SCANNED, []), ecr_images=[])
    with pytest.raises(ImageScanPendingError, match="pulled into the pull-through cache"):
        _scan(image, _Inspector(SCANNED, []), ecr_images=None)
    assert filled[:1] == [
        (HOST, "agentic-platform-ecr-public/docker/library/python", DIGEST, "token")
    ]


def _admission() -> Admission:
    images = (
        admit(f"{HOST}/agentic-platform/semgrep:1@{DIGEST}", POLICY, "adp"),
        admit(f"{HOST}/agentic-platform/tenants/adp/checks@{DIGEST}", POLICY, "adp"),
        admit(f"python:3.13@{DIGEST}", POLICY, "adp", MIRRORS),
    )
    return Admission(images=images, policy=POLICY, tenant="adp", mirrors=MIRRORS)


def test_the_pod_policy_rejects_every_registry_the_image_policy_does_not_admit() -> None:
    policy = json.loads(containers_policy(_admission()))

    assert policy["default"] == [{"type": "reject"}]
    scopes = policy["transports"]["docker"]
    assert f"{HOST}/agentic-platform/tenants/adp" in scopes
    assert "docker.io/library" in scopes
    assert not any("tenants/other" in scope for scope in scopes)


def test_the_pod_verifies_each_signed_image_against_its_own_profile() -> None:
    admission = _admission()
    trust = json.loads(trust_policy(admission))

    identities = {p["registryScopes"][0]: p["trustedIdentities"] for p in trust["trustPolicies"]}
    assert identities == {
        f"{HOST}/agentic-platform/semgrep": [POLICY.signatures.platform_profile],
        f"{HOST}/agentic-platform/tenants/adp/checks": [
            "arn:aws:signer:us-east-1:123456789012:/signing-profiles/t_adp"
        ],
    }
    script = verify_script(admission)
    assert script.count("notation verify") == 2
    assert "python" not in script


def test_the_scan_wait_is_capped_at_half_the_activitys_remaining_time() -> None:
    import dataclasses
    from datetime import UTC, datetime, timedelta

    from temporalio.testing import ActivityEnvironment

    environment = ActivityEnvironment()
    environment.info = dataclasses.replace(
        environment.info,
        start_to_close_timeout=timedelta(minutes=20),
        started_time=datetime.now(UTC) - timedelta(minutes=16),
    )

    budget = environment.run(image_admission.scan_wait_budget)

    assert 110 < budget <= 120
    assert image_admission.scan_wait_budget() == 600


async def test_a_scan_still_pending_at_the_deadline_fails_with_its_reason(monkeypatch) -> None:
    import asyncio

    def pending(*args, **kwargs):
        raise ImageScanPendingError("scan of python pending")

    monkeypatch.setattr(image_admission, "require_clean_scan", pending)
    image = admit(f"python:3.13@{DIGEST}", POLICY, "adp", MIRRORS)
    deadline = asyncio.get_running_loop().time()

    with pytest.raises(ImageScanPendingError, match="scan of python pending"):
        await image_admission._await_clean_scan(image, POLICY, [], deadline)


def test_the_trust_policy_file_is_the_one_the_pinned_notation_reads() -> None:
    import re
    from pathlib import Path

    dockerfile = (Path(__file__).resolve().parents[2] / "docker/buildah.Dockerfile").read_text()
    version = re.search(r"^ARG NOTATION_VERSION=v?(\S+)$", dockerfile, re.M).group(1)

    # notation 1.3 reads trustpolicy.json and ignores trustpolicy.oci.json, so upgrading notation
    # means confirming which file the new release reads and changing TRUST_POLICY_FILE with it.
    assert version.startswith("1.3.")
    assert image_admission.TRUST_POLICY_FILE == "trustpolicy.json"


def test_an_engine_base_image_on_ecr_public_resolves_through_the_cache() -> None:
    cache = f"{HOST}/agentic-platform-ecr-public"
    mirrors = {**MIRRORS, "public.ecr.aws": cache}

    admitted = admit(f"public.ecr.aws/docker/library/python@{DIGEST}", POLICY, "adp", mirrors)
    scopes = json.loads(
        containers_policy(
            Admission(images=[admitted], policy=POLICY, tenant="adp", mirrors=mirrors)
        )
    )["transports"]["docker"]

    assert admitted.resolved.name == f"{cache}/docker/library/python"
    assert "public.ecr.aws" in scopes
