"""The claims and slack groups of `inv tenant-verify-aws`, which tenant_verify_aws.verify runs.

Their pod probes, poster-tls, action-key-fingerprint, and action-key-fingerprints, stay in
tenant_verify_aws.py, because a probe runs that file's source inside the pod.
"""

from __future__ import annotations

from dataclasses import dataclass

from scripts.tenant_verify_aws import (
    ABSENT,
    ALLOWED,
    BUCKET_LIST,
    DENIED,
    FAIL,
    PASS,
    PLATFORM_NAMESPACE,
    PRESENT,
    WORKER,
    Aws,
    Check,
    CommandError,
    Deployment,
    Kube,
    Principal,
    Request,
    compare,
    failed,
    simulated,
    skipped,
)

LEAF_DAYS = 30
PROTOCOL_DENY = "DenyBundleUploadsWithoutTheClaimsProtocolTag"
PUBLISHER_TAG = "aws:PrincipalTag/tenant-config-publisher"
SAMPLE_CHANNEL = "C0TENANTVERIFY"
MATCHES = "matches"


@dataclass(frozen=True)
class _BucketPolicy:
    """The config bucket's policy, and the request context every simulated S3 call carries."""

    bucket_arn: str
    policy: str
    owner: str

    def request(self, action: str, resource: str, **context: tuple[str, str]) -> Request:
        tls = {"aws:SecureTransport": ("true", "boolean")}
        return Request(action, resource, self.policy, self.owner, {**tls, **context})


def sample_claim_prefix() -> str:
    from services.integration.tenant_claims import SLACK_CHANNEL, Claim

    return Claim(SLACK_CHANNEL, SAMPLE_CHANNEL).prefix


def claims_checks(deployment: Deployment, aws: Aws) -> list[Check]:
    """Simulate the claim protocol's writes, listings, and reads against the bucket policy."""
    from services.integration.tenant_claims import CLAIMS_PROTOCOL_KEY, CLAIMS_PROTOCOL_TAG

    if not deployment.config_bucket:
        return [skipped("claims", "config bucket", "claims", "the outputs name no bucket")]
    try:
        policy = aws.bucket_policy(deployment.config_bucket)
    except CommandError as exc:
        return [failed("claims", "config bucket", "bucket policy", PRESENT, str(exc))]
    bucket = _BucketPolicy(
        f"arn:{deployment.partition}:s3:::{deployment.config_bucket}",
        policy,
        deployment.account_root,
    )
    markers = f"{bucket.bucket_arn}/{sample_claim_prefix()}"
    tag_key, _, tag_value = CLAIMS_PROTOCOL_TAG.partition("=")
    enforced = PROTOCOL_DENY in policy
    checks: list[Check] = []
    for tenant in deployment.tenants.values():
        subject = f"{tenant.name}/claims"
        publisher_arn = deployment.publishers.get(tenant.name)
        if not publisher_arn:
            checks.append(skipped("claims", subject, "publisher", "the tenant has no publisher"))
            continue
        publisher = Principal(f"{tenant.name} publisher", publisher_arn)
        tagged = {PUBLISHER_TAG: (tenant.name, "string")}
        own = bucket.request("s3:DeleteObject", f"{markers}{tenant.name}", **tagged)
        checks += simulated(aws, subject, own, [(publisher, ALLOWED)], group="claims")
        for other in deployment.others(tenant.name):
            foreign = bucket.request("s3:DeleteObject", f"{markers}{other.name}", **tagged)
            checks += simulated(aws, subject, foreign, [(publisher, DENIED)], group="claims")
        upload = f"{bucket.bucket_arn}/tenants/{tenant.name}/escalation.yaml"
        if enforced:
            protocol_tag = {f"s3:RequestObjectTag/{tag_key}": (tag_value, "string")}
            untagged = bucket.request("s3:PutObject", upload, **tagged)
            with_tag = bucket.request("s3:PutObject", upload, **tagged, **protocol_tag)
            checks += simulated(
                aws, f"{subject} untagged", untagged, [(publisher, DENIED)], group="claims"
            )
            tagging = bucket.request("s3:PutObjectTagging", upload, **tagged, **protocol_tag)
            checks += simulated(
                aws, f"{subject} tagged", with_tag, [(publisher, ALLOWED)], group="claims"
            )
            checks += simulated(
                aws, f"{subject} tagging", tagging, [(publisher, ALLOWED)], group="claims"
            )
        else:
            checks.append(skipped("claims", subject, "untagged upload", "enforcement is off"))
        checks += probe_role_checks(deployment, aws, tenant.name, bucket)
    readers = [
        *(
            Principal(f"{name} publisher", arn)
            for name, arn in sorted(deployment.publishers.items())
        ),
        *(
            Principal(f"{name} claims probe", arn)
            for name, arn in sorted(deployment.probes.items())
        ),
        *(
            Principal(workload, deployment.pod_roles[workload])
            for workload in ("inbound_webhook", "slack_poster")
            if workload in deployment.pod_roles
        ),
    ]
    read = bucket.request("s3:GetObject", f"{bucket.bucket_arn}/{CLAIMS_PROTOCOL_KEY}")
    checks += simulated(
        aws, CLAIMS_PROTOCOL_KEY, read, [(r, ALLOWED) for r in readers], group="claims"
    )
    try:
        exists = aws.object_exists(deployment.config_bucket, CLAIMS_PROTOCOL_KEY)
    except CommandError as exc:
        checks.append(failed("claims", CLAIMS_PROTOCOL_KEY, "object", PRESENT, str(exc)))
    else:
        checks.append(
            compare("claims", CLAIMS_PROTOCOL_KEY, "object", PRESENT, PRESENT if exists else ABSENT)
        )
    return checks


def probe_role_checks(
    deployment: Deployment, aws: Aws, tenant: str, bucket: _BucketPolicy
) -> list[Check]:
    from services.integration.tenant_claims import CLAIMS_PREFIX

    subject = f"{tenant}/claims probe"
    arn = deployment.probes.get(tenant)
    if not arn:
        return [skipped("claims", subject, "probe role", "the tenant has no claims probe")]
    probe = Principal(f"{tenant} claims probe", arn)
    try:
        trust = aws.role_trust(arn)
    except CommandError as exc:
        return [failed("claims", subject, "trust policy", "web identity", str(exc))]
    actions = {
        action
        for statement in trust.get("Statement") or []
        for action in (
            [statement.get("Action")]
            if isinstance(statement.get("Action"), str)
            else statement.get("Action") or []
        )
    }
    checks = [
        compare(
            "claims",
            subject,
            "trust policy",
            "sts:AssumeRoleWithWebIdentity",
            ",".join(sorted(actions)) or "none",
        )
    ]
    one = bucket.request(
        BUCKET_LIST, bucket.bucket_arn, **{"s3:prefix": (sample_claim_prefix(), "string")}
    )
    every = bucket.request(
        BUCKET_LIST, bucket.bucket_arn, **{"s3:prefix": (f"{CLAIMS_PREFIX}/", "string")}
    )
    checks += simulated(aws, subject, one, [(probe, ALLOWED)], group="claims")
    checks += simulated(aws, f"{subject} every claim", every, [(probe, DENIED)], group="claims")
    return checks


def slack_checks(deployment: Deployment, kube: Kube) -> list[Check]:
    """Check the posting service is ready and each tenant worker verifies its certificate."""
    from scripts.webhook_eks_render import SLACK_POSTER

    try:
        platform = {d["metadata"]["name"]: d for d in kube.deployments(PLATFORM_NAMESPACE)}
    except CommandError as exc:
        return [failed("slack", PLATFORM_NAMESPACE, "deployments", "a list", str(exc))]
    poster = platform.get(SLACK_POSTER)
    if poster is None:
        return [skipped("slack", SLACK_POSTER, "posting service", "no shared Slack app deployed")]
    ready = int((poster.get("status") or {}).get("readyReplicas") or 0)
    checks = [compare("slack", SLACK_POSTER, "ready replicas", "1", str(min(ready, 1)))]
    for tenant in deployment.tenants.values():
        subject = f"{tenant.namespace}/{WORKER}"
        try:
            report = kube.probe(tenant.namespace, WORKER, WORKER, ["poster-tls"], entrypoint=True)
        except CommandError as exc:
            checks.append(
                failed("slack", subject, "posting service TLS", "a probe result", str(exc))
            )
            continue
        if report.get("error"):
            checks.append(failed("slack", subject, "verified /ready", "200", str(report["error"])))
            continue
        checks.append(compare("slack", subject, "verified /ready", "200", str(report.get("ready"))))
        days = int(report.get("days_left") or 0)
        status = PASS if days >= LEAF_DAYS else FAIL
        checks.append(
            Check("slack", subject, "certificate days left", f">= {LEAF_DAYS}", str(days), status)
        )
    return checks + fingerprint_checks(deployment, kube)


def fingerprint_checks(deployment: Deployment, kube: Kube) -> list[Check]:
    """Check that the posting service holds each tenant's action key fingerprint, so it refuses
    the tenants that share a key as the webhook does."""
    from scripts.webhook_eks_render import SLACK_POSTER

    try:
        held = kube.probe(
            PLATFORM_NAMESPACE,
            SLACK_POSTER,
            SLACK_POSTER,
            ["action-key-fingerprints"],
            entrypoint=True,
        )
    except CommandError as exc:
        return [
            failed("slack", SLACK_POSTER, "action key fingerprints", "a probe result", str(exc))
        ]
    checks = []
    for tenant in deployment.tenants.values():
        subject = f"{tenant.namespace}/{WORKER}"
        try:
            report = kube.probe(
                tenant.namespace, WORKER, WORKER, ["action-key-fingerprint"], entrypoint=True
            )
        except CommandError as exc:
            checks.append(
                failed("slack", subject, "action key fingerprint", "a probe result", str(exc))
            )
            continue
        if report.get("error"):
            checks.append(
                failed("slack", subject, "action key fingerprint", MATCHES, str(report["error"]))
            )
            continue
        expected = held.get(tenant.name)
        if expected is None:
            observed = "absent from the posting service"
        elif expected == report.get("fingerprint"):
            observed = MATCHES
        else:
            observed = "differs, so raise the tenant's escalation_action_key_version"
        checks.append(compare("slack", subject, "action key fingerprint", MATCHES, observed))
    return checks
