"""Admit the images a tenant pod pulls, and hand the pod the rules its own pull enforces.

Before a gate, checks, scanner, or sandbox pod pulls an image, the tenant's worker admits it:

- the image policy must admit the reference, which must carry a digest
- with `scan.gate: inspector`, Amazon Inspector must have scanned the image the pod's platform
  resolves to, and it must carry no active finding at or above `scan.severity` that
  image_exceptions.yaml does not accept

A refused image fails the pod's step closed. A scan that has not completed, including a cache image
the worker has just pulled into its pull-through cache, raises `ImageScanPendingError`, which the
step retries until the scan completes.

The pod enforces the same allowlist itself through a buildah policy.json that rejects every other
registry, and with signature verification on, it verifies each signed image with notation and the
AWS Signer plugin immediately before the pull.
"""

from __future__ import annotations

import asyncio
import json
import platform
import re
import urllib.request
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from adapters.interfaces.security_scanner import ScannerUnavailableError
from temporalio.exceptions import ApplicationError

from services.integration.credentials import ConfigError
from services.integration.image_policy import (
    CACHE,
    CATALOG,
    INSPECTOR,
    TENANT,
    AdmittedImage,
    FindingException,
    ImagePolicy,
    accepted,
    admit,
    admits_mirror,
    load_exceptions,
    load_policy,
)

_ECR_HOST = re.compile(
    r"^(?P<account>\d{12})\.dkr\.ecr(?:-fips)?\.(?P<region>[a-z0-9-]+)\.amazonaws\.com(?P<cn>\.cn)?$"
)
_INDEX_TYPES = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
)
_MANIFEST_TYPES = (
    *_INDEX_TYPES,
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
)
_ARCHES = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
_LISTED_FINDINGS = 10
_SCAN_POLL_SECONDS = 30
NOTATION_CONFIG_DIR = "/root/.config/notation"
TRUST_POLICY_FILE = "trustpolicy.json"
SIGNER_TRUST_STORE = "signingAuthority:aws-signer-ts"
POLICY_DIR = "/etc/containers"
POLICY_FILE = "policy.json"
VERIFIER_SERVICE_ACCOUNT = "image-verifier"


IMAGE_REFUSED = "ImageRefused"


class ImageRefusedError(ApplicationError):
    """The image policy or the image's scan refuses the image, which no retry changes."""

    def __init__(self, message: str) -> None:
        super().__init__(message, type=IMAGE_REFUSED, non_retryable=True)


class ImageScanPendingError(ScannerUnavailableError):
    """The image has no completed scan yet, so the step retries until it has one."""


@dataclass(frozen=True)
class Admission:
    """The images a pod may pull, and what its pull must verify."""

    images: tuple[AdmittedImage, ...]
    policy: ImagePolicy
    tenant: str
    mirrors: Mapping[str, str] = field(default_factory=dict)

    def signed(self) -> list[tuple[AdmittedImage, str]]:
        """Return each image whose signature the pod verifies, with its signing profile."""
        if not self.policy.signatures.verification:
            return []
        signed = []
        for image in self.images:
            if image.kind not in (CATALOG, TENANT):
                continue
            profile = self.policy.signatures.profile_for(image.kind, self.tenant)
            if not profile:
                raise ImageRefusedError(
                    f"signature verification is on, and image_policy.yaml names no signing "
                    f"profile for {image.kind} image {image.requested}"
                )
            signed.append((image, profile))
        return signed


def _environment() -> tuple[str, str, Mapping[str, str]]:
    from scripts.config import get_settings

    from services.orchestration.gates import registry_mirrors

    settings = get_settings()
    config = registry_mirrors.current()
    return settings.environment, settings.tenant, dict(config.mirrors) if config else {}


async def admit_images(images: Iterable[str]) -> Admission:
    """Admit every image or raise ImageRefusedError, or ImageScanPendingError for a pending scan."""
    environment, tenant, mirrors = _environment()
    try:
        policy = load_policy(environment)
        exceptions = load_exceptions(environment)
        admitted = tuple(admit(image, policy, tenant, mirrors) for image in dict.fromkeys(images))
    except ConfigError as exc:
        raise ImageRefusedError(str(exc)) from exc
    if policy.scan_gate == INSPECTOR:
        deadline = asyncio.get_running_loop().time() + scan_wait_budget()
        for image in admitted:
            await _await_clean_scan(image, policy, exceptions, deadline)
    return Admission(images=admitted, policy=policy, tenant=tenant, mirrors=mirrors)


def scan_wait_budget() -> float:
    """Return how long the pod's images may wait for pending scans, all of them together.

    The wait is IMAGE_SCAN_WAIT_SECONDS, capped at half the time the activity has left, so the
    step that pulls the images still has time to run, and a scan that stays pending fails the
    step with its reason before the activity times out.
    """
    from scripts.config import get_settings
    from temporalio import activity

    budget = float(get_settings().image_scan_wait_seconds)
    if activity.in_activity():
        info = activity.info()
        if info.start_to_close_timeout:
            elapsed = (datetime.now(UTC) - info.started_time).total_seconds()
            remaining = info.start_to_close_timeout.total_seconds() - elapsed
            budget = min(budget, max(0.0, remaining / 2))
    return budget


async def _await_clean_scan(
    image: AdmittedImage,
    policy: ImagePolicy,
    exceptions: Sequence[FindingException],
    deadline: float,
) -> None:
    """Wait until the deadline for a pending scan, heartbeating while it waits."""
    loop = asyncio.get_running_loop()
    while True:
        try:
            await asyncio.to_thread(require_clean_scan, image, policy, exceptions)
            return
        except ImageScanPendingError as exc:
            if loop.time() >= deadline:
                raise
            _heartbeat(str(exc))
            await asyncio.sleep(_SCAN_POLL_SECONDS)


def _heartbeat(detail: str) -> None:
    from temporalio import activity

    if activity.in_activity():
        activity.heartbeat(detail)


def _partition(region: str, china: bool) -> str:
    if china:
        return "aws-cn"
    return "aws-us-gov" if region.startswith("us-gov-") else "aws"


def node_arch() -> str:
    """Return the architecture a pod runs on, which the cluster's nodes share with the worker."""
    machine = platform.machine().lower()
    return _ARCHES.get(machine, machine)


def _platform_digest(manifest: Mapping[str, Any], arch: str) -> str:
    for entry in manifest.get("manifests") or []:
        target = entry.get("platform") or {}
        if target.get("os") == "linux" and target.get("architecture") == arch:
            return str(entry["digest"])
    return ""


def _fill_cache(host: str, repository: str, digest: str, token: str) -> None:
    """Pull the manifest through the registry API, which makes ECR import it into the cache."""
    request = urllib.request.Request(
        f"https://{host}/v2/{repository}/manifests/{digest}",
        headers={"Authorization": f"Basic {token}", "Accept": ", ".join(_MANIFEST_TYPES)},
    )
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - https only
        response.read()


def require_clean_scan(
    image: AdmittedImage,
    policy: ImagePolicy,
    exceptions: Sequence[FindingException],
    *,
    clients: Any = None,
    today: Any = None,
    arch: str = "",
) -> None:
    """Refuse the image unless Inspector scanned it and found nothing the policy blocks."""
    import boto3

    resolved = image.resolved
    match = _ECR_HOST.match(resolved.registry)
    if match is None:
        raise ImageRefusedError(
            f"the image {image.requested} resolves to {resolved.name}, outside ECR, so Amazon "
            "Inspector cannot scan it and the scan gate refuses it"
        )
    account, region = match["account"], match["region"]
    ecr = clients["ecr"] if clients else boto3.client("ecr", region_name=region)
    inspector = clients["inspector2"] if clients else boto3.client("inspector2", region_name=region)
    try:
        response = ecr.batch_get_image(
            registryId=account,
            repositoryName=resolved.repository,
            imageIds=[{"imageDigest": resolved.digest}],
            acceptedMediaTypes=list(_MANIFEST_TYPES),
        )
    except ecr.exceptions.RepositoryNotFoundException:
        # A pull-through cache creates an upstream's repository on its first pull.
        response = {}
    if not response.get("images"):
        if image.kind != CACHE:
            raise ImageRefusedError(
                f"the image {image.requested} is not in ECR at {resolved.name}@{resolved.digest}"
            )
        from services.orchestration.gates.registry_auth import ecr_token

        _fill_cache(
            resolved.registry, resolved.repository, resolved.digest, ecr_token(account, region)
        )
        raise ImageScanPendingError(
            f"{image.requested} was pulled into the pull-through cache, and Amazon Inspector has "
            "not scanned it yet. The step retries until the scan completes."
        )
    found = response["images"][0]
    digest = resolved.digest
    if found.get("imageManifestMediaType") in _INDEX_TYPES:
        target = arch or node_arch()
        digest = _platform_digest(json.loads(found["imageManifest"]), target)
        if not digest:
            raise ImageRefusedError(
                f"the image {image.requested} has no linux/{target} image for the cluster's nodes"
            )
    partition = _partition(region, bool(match["cn"]))
    resource = f"arn:{partition}:ecr:{region}:{account}:repository/{resolved.repository}/{digest}"
    criteria = {"resourceId": [{"comparison": "EQUALS", "value": resource}]}
    covered = inspector.list_coverage(filterCriteria=criteria).get("coveredResources") or []
    status = (covered[0].get("scanStatus") or {}) if covered else {}
    if status.get("statusCode") != "ACTIVE" or status.get("reason") != "SUCCESSFUL":
        reason = status.get("reason") or "no scan recorded"
        raise ImageScanPendingError(
            f"Amazon Inspector has not completed a scan of {image.requested} for linux/"
            f"{arch or node_arch()} ({reason}). The step retries until the scan completes."
        )
    findings = _blocking_findings(inspector, resource, policy)
    day = today or datetime.now(UTC).date()
    open_findings = sorted(
        finding
        for finding in findings
        if accepted(exceptions, image.requested, finding, day) is None
        and accepted(exceptions, image.resolved, finding, day) is None
    )
    if open_findings:
        listed = ", ".join(open_findings[:_LISTED_FINDINGS])
        more = len(open_findings) - _LISTED_FINDINGS
        rest = f" and {more} more" if more > 0 else ""
        raise ImageRefusedError(
            f"the image {image.requested} carries {len(open_findings)} active finding(s) at "
            f"{policy.severity} or above: {listed}{rest}. Rebuild it on a patched base, or accept "
            "each finding in image_exceptions.yaml with a reason and an expiry."
        )


def _blocking_findings(inspector: Any, resource: str, policy: ImagePolicy) -> set[str]:
    criteria = {
        "resourceId": [{"comparison": "EQUALS", "value": resource}],
        "findingStatus": [{"comparison": "EQUALS", "value": "ACTIVE"}],
        "severity": [{"comparison": "EQUALS", "value": level} for level in policy.blocking()],
    }
    findings: set[str] = set()
    token = None
    while True:
        kwargs = {"filterCriteria": criteria, "maxResults": 100}
        if token:
            kwargs["nextToken"] = token
        page = inspector.list_findings(**kwargs)
        for finding in page.get("findings") or []:
            details = finding.get("packageVulnerabilityDetails") or {}
            findings.add(str(details.get("vulnerabilityId") or finding.get("title") or "unknown"))
        token = page.get("nextToken")
        if not token:
            return findings


def containers_policy(admission: Admission) -> str:
    """Return the buildah policy.json that admits only the policy's registries.

    The policy rejects every registry reference by default and accepts each admitted repository,
    so a Dockerfile FROM line or a pull the script did not plan is refused by the pull itself.
    The pod's own image store stays readable, because a built image is used from it.
    """
    scopes: dict[str, list[dict[str, str]]] = {}
    accept = [{"type": "insecureAcceptAnything"}]
    for entry, _ in admission.policy.entries(admission.tenant):
        scopes[entry.rstrip("/")] = accept
    # A FROM line names the upstream, and registries.conf sends the pull to its mirror, so an
    # upstream whose mirror the policy admits is admitted under its own name.
    for upstream, location in admission.mirrors.items():
        if admits_mirror(location, admission.policy, admission.tenant):
            scopes[upstream.rstrip("/")] = accept
            scopes[location.rstrip("/")] = accept
    for image in admission.images:
        scopes[image.requested.name] = accept
        scopes[image.resolved.name] = accept
    return json.dumps(
        {
            "default": [{"type": "reject"}],
            "transports": {
                "docker": dict(sorted(scopes.items())),
                "containers-storage": {"": accept},
                "dir": {"": accept},
                "oci": {"": accept},
            },
        },
        indent=2,
    )


def trust_policy(admission: Admission) -> str:
    """Return the notation trust policy that ties each signed repository to its signing profile."""
    policies = []
    for image, profile in admission.signed():
        policies.append(
            {
                "name": f"{image.kind}-{len(policies)}",
                "registryScopes": [image.resolved.name],
                "signatureVerification": {"level": "strict"},
                "trustStores": [SIGNER_TRUST_STORE],
                "trustedIdentities": [profile],
            }
        )
    return json.dumps({"version": "1.0", "trustPolicies": policies}, indent=2)


def verify_script(admission: Admission) -> str:
    """Return the shell lines that verify each signed image before the pod pulls it."""
    return "".join(
        f"notation verify {image.resolved.name}@{image.resolved.digest} >&2\n"
        for image, _ in admission.signed()
    )


def pod_service_account() -> str:
    """Return the ServiceAccount a pull pod runs as, which carries the verifier's identity.

    The identity may only read a signature's revocation status, so a pod runs as it only while
    signature verification is on.
    """
    from scripts.config import get_settings

    try:
        policy = load_policy(get_settings().environment)
    except ConfigError:
        return ""
    return VERIFIER_SERVICE_ACCOUNT if policy.signatures.verification else ""


async def admit_sandbox_image(image: str, settings: Any, ops: Any = None) -> None:
    """Admit the sandbox image the kubelet pulls, and verify its signature in a pod first.

    The kubelet pulls a sandbox pod's image before any of its code runs, so the signature is
    verified by a short-lived pod in the tenant's sandbox namespace, which runs as the verifier
    identity and pulls nothing but the signature. The sandbox pod then pulls the same digest.
    """
    from uuid import uuid4

    from services.orchestration.gates import registry_auth
    from services.orchestration.gates.pod_runner import (
        KubectlPodOps,
        build_pod_manifest,
        pod_name_for,
    )

    admission = await admit_images([image])
    if not admission.signed():
        return
    ops = ops or KubectlPodOps()
    namespace = settings.openhands_sandbox_namespace
    pod = pod_name_for("sandbox", uuid4().hex[:12], prefix="image-verify")
    manifest = build_pod_manifest(
        pod,
        namespace,
        settings.gate_engine_builder_image,
        node_pool=settings.sandbox_node_pool,
        service_account=VERIFIER_SERVICE_ACCOUNT,
    )
    await ops.create(manifest)
    try:
        await ops.wait_ready(pod, namespace)
        setup = await registry_auth.install(ops, pod, namespace, [image])
        code, _, stderr = await ops.exec_capture(
            pod, namespace, f"set -e\n{setup.before_pull()}{setup.after_pull()}"
        )
        if code != 0:
            detail = stderr.strip()[-800:]
            raise ImageRefusedError(
                f"the signature of the sandbox image {image} did not verify: {detail}"
            )
    finally:
        await ops.delete(pod, namespace)
