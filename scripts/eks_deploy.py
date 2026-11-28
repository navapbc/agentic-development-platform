"""Run the eks-deployer task, which deploys the EKS workloads from inside the VPC.

The workstation needs no kubectl, helm, or route to the private Kubernetes API. This reads the
`eks` output of the composition root, checks which secrets the overlay mounts hold a current value,
and starts the eks-deployer one-shot task on the platform ECS cluster with the `deploy` command and
the secrets to leave out. A pod whose SecretProviderClass names a secret without a value never
starts, so a workload runs without an external secret its environment has not set yet. A tenant's
review worker is the exception, because it refuses to start without its review signing key while
review signing is enabled, so the deploy names each tenant whose key is unset. The task's log is
printed once it stops.

When the output names MCP gateway hosts, the deploy first checks that the gateway certificate
covers each host, and after the task succeeds it points each tenant's host in the gateway zone at
the load balancer the AWS Load Balancer Controller created for that tenant's gateway Ingress. The
task has no route to the Route 53 API and its role holds no AWS permission, so the workstation
credentials write the records.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml

from scripts.eks_render import RenderError, require_platform_escalation, secret_key
from scripts.temporal_certs import terraform_output
from scripts.tenant_render import GATEWAY, worker_namespace

REPO_ROOT = Path(__file__).resolve().parent.parent
PROVIDERS = REPO_ROOT / "deploy" / "kubernetes" / "aws" / "secret-provider-classes.yaml"
TASK = "eks-deployer"
# A deploy can wait through a worker's 2460-second termination grace while its activities finish.
TASK_WAIT = {"Delay": 10, "MaxAttempts": 540}
LOG = "[eks-deploy]"
REVIEW_SIGNING_KEY = "review_signing_key"
CLUSTER_TAG = "elbv2.k8s.aws/cluster"
INGRESS_STACK_TAG = "ingress.k8s.aws/stack"


class DeployError(Exception):
    """The eks-deployer task could not run or did not succeed."""


def mounted_secret_keys(providers: Path = PROVIDERS) -> list[str]:
    keys: set[str] = set()
    for document in yaml.safe_load_all(providers.read_text(encoding="utf-8")):
        if document:
            for entry in yaml.safe_load(document["spec"]["parameters"]["objects"]):
                keys.add(secret_key(entry["objectName"]))
    return sorted(keys)


def _has_value(arn: str, secretsmanager: Any) -> bool:
    stages = secretsmanager.describe_secret(SecretId=arn).get("VersionIdsToStages") or {}
    return any("AWSCURRENT" in labels for labels in stages.values())


def unset_secrets(keys: list[str], arns: dict[str, str], secretsmanager: Any) -> list[str]:
    """Return the keys whose secret has no AWSCURRENT version, which no pod could mount."""
    unset = []
    for key in keys:
        arn = arns.get(key)
        if not arn:
            raise DeployError(f"the eks output names no secret for {key}")
        if not _has_value(arn, secretsmanager):
            unset.append(key)
    return unset


def unset_tenant_secrets(tenants: list[dict[str, Any]], secretsmanager: Any) -> list[str]:
    """Return `<tenant>:<key>` for each tenant secret without an AWSCURRENT version."""
    return [
        f"{tenant['name']}:{key}"
        for tenant in tenants
        for key, arn in sorted((tenant.get("secrets") or {}).items())
        if not _has_value(arn, secretsmanager)
    ]


def unset_message(key: str) -> str:
    tenant, _, name = key.rpartition(":")
    if tenant and name == REVIEW_SIGNING_KEY:
        return (
            f"{key} has no value, so the review worker of {tenant} refuses to start while review "
            f"signing is enabled. Write the key to the tenant's review-signing-key secret and add "
            f"its public key to the trust root under tenant {tenant}."
        )
    return f"{key} has no value, so the pods that mount it start without it"


def platform_mounted_keys(
    mounted: list[str], arns: dict[str, str], tenants: list[dict[str, Any]]
) -> list[str]:
    """Return the mounted keys the platform secrets must cover.

    A key the platform has no secret for, which every tenant holds as its own secret, mounts only
    in tenant namespaces, such as the token of each tenant's MCP gateway.
    """
    tenant_owned = {
        key
        for key in mounted
        if key not in arns
        and tenants
        and all(key in (tenant.get("secrets") or {}) for tenant in tenants)
    }
    return [key for key in mounted if key not in tenant_owned]


def run_deployer(ecs: Any, logs: Any, deployer: dict[str, Any], command: list[str]) -> int:
    task = deployer["tasks"][TASK]
    started = ecs.run_task(
        cluster=deployer["cluster"],
        taskDefinition=task["task_definition"],
        capacityProviderStrategy=[{"capacityProvider": "FARGATE", "weight": 1}],
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": deployer["task_subnets"],
                "securityGroups": task["security_groups"],
                "assignPublicIp": "DISABLED",
            }
        },
        overrides={"containerOverrides": [{"name": task["container"], "command": command}]},
        startedBy="eks-deploy",
    )
    if started.get("failures"):
        raise DeployError(f"the eks-deployer task did not start: {started['failures']}")
    task_arn = started["tasks"][0]["taskArn"]
    task_id = task_arn.rsplit("/", 1)[-1]
    print(f"{LOG} running {' '.join(command)} as task {task_id}")
    ecs.get_waiter("tasks_stopped").wait(
        cluster=deployer["cluster"], tasks=[task_arn], WaiterConfig=TASK_WAIT
    )
    stopped = ecs.describe_tasks(cluster=deployer["cluster"], tasks=[task_arn])["tasks"][0]
    container = next(c for c in stopped["containers"] if c["name"] == task["container"])
    print_log(logs, deployer["log_group"], f"{task['container']}/{task['container']}/{task_id}")
    exit_code = container.get("exitCode")
    if exit_code != 0:
        raise DeployError(
            f"the eks-deployer task exited with {exit_code} "
            f"({stopped.get('stoppedReason', 'no reason')})"
        )
    return exit_code


def print_log(logs: Any, group: str, stream: str) -> None:
    try:
        paginator = logs.get_paginator("filter_log_events")
        for page in paginator.paginate(logGroupName=group, logStreamNames=[stream]):
            for event in page["events"]:
                print(event["message"])
    except logs.exceptions.ResourceNotFoundException:
        print(f"{LOG} the task wrote no log to {group}")


def _terraform_eks(env: dict[str, str]) -> dict[str, Any] | None:
    return json.loads(terraform_output("eks", env, DeployError) or "null")


def certificate_covers(names: Iterable[str], host: str) -> bool:
    """Return whether a certificate for `names` covers `host`. A wildcard covers one label."""
    host = host.lower().rstrip(".")
    parent = host.partition(".")[2]
    return any(
        name in (host, f"*.{parent}") if parent else name == host
        for name in (name.lower().rstrip(".") for name in names)
    )


def check_gateway_certificate(acm: Any, arn: str, hosts: Mapping[str, str]) -> None:
    """Refuse a gateway certificate that does not cover each tenant's gateway host."""
    certificate = acm.describe_certificate(CertificateArn=arn)["Certificate"]
    names = [certificate["DomainName"], *certificate.get("SubjectAlternativeNames", [])]
    uncovered = sorted(host for host in hosts.values() if not certificate_covers(names, host))
    if uncovered:
        raise DeployError(
            f"the MCP gateway certificate {arn} covers {', '.join(sorted(set(names)))} "
            f"and not {', '.join(uncovered)}"
        )


def gateway_load_balancers(
    elbv2: Any, cluster: str, vpc_id: str, tenants: Iterable[str]
) -> dict[str, dict[str, Any]]:
    """Return the load balancer of each tenant's gateway Ingress, by tenant."""
    stacks = {f"{worker_namespace(tenant)}/{GATEWAY}": tenant for tenant in tenants}
    balancers = [
        balancer
        for page in elbv2.get_paginator("describe_load_balancers").paginate()
        for balancer in page["LoadBalancers"]
        if balancer.get("VpcId") == vpc_id
    ]
    found: dict[str, dict[str, Any]] = {}
    for start in range(0, len(balancers), 20):
        by_arn = {b["LoadBalancerArn"]: b for b in balancers[start : start + 20]}
        for description in elbv2.describe_tags(ResourceArns=list(by_arn))["TagDescriptions"]:
            tags = {tag["Key"]: tag["Value"] for tag in description.get("Tags", [])}
            if tags.get(CLUSTER_TAG) == cluster and tags.get(INGRESS_STACK_TAG) in stacks:
                found[stacks[tags[INGRESS_STACK_TAG]]] = by_arn[description["ResourceArn"]]
    return found


def publish_gateway_hosts(
    route53: Any,
    zone_id: str,
    hosts: Mapping[str, str],
    balancers: Mapping[str, Mapping[str, Any]],
) -> None:
    """Point each tenant's gateway host at its load balancer with an alias record."""
    missing = sorted(set(hosts) - set(balancers))
    if missing:
        raise DeployError(f"no load balancer serves the MCP gateway of {', '.join(missing)}")
    route53.change_resource_record_sets(
        HostedZoneId=zone_id,
        ChangeBatch={
            "Comment": "MCP gateway load balancer of each tenant",
            "Changes": [
                {
                    "Action": "UPSERT",
                    "ResourceRecordSet": {
                        "Name": hosts[tenant],
                        "Type": "A",
                        "AliasTarget": {
                            "HostedZoneId": balancers[tenant]["CanonicalHostedZoneId"],
                            "DNSName": balancers[tenant]["DNSName"],
                            "EvaluateTargetHealth": False,
                        },
                    },
                }
                for tenant in sorted(hosts)
            ],
        },
    )
    for tenant in sorted(hosts):
        print(f"{LOG} {hosts[tenant]} names the {tenant} MCP gateway load balancer")


def deploy(session: Any, eks: dict[str, Any] | None, command: list[str]) -> int:
    if not eks or not eks.get("deployer"):
        print(f"{LOG} the composition root has no EKS cluster or eks-deployer image, skipping")
        return 0
    hosts = (eks.get("mcp_gateway_hosts") or {}) if command[0] == "deploy" else {}
    if hosts:
        check_gateway_certificate(session.client("acm"), eks["mcp_gateway_certificate_arn"], hosts)
    if command[0] == "deploy":
        try:
            require_platform_escalation(eks.get("environment_config") or {})
        except RenderError as exc:
            raise DeployError(str(exc)) from None
        secretsmanager = session.client("secretsmanager")
        tenants = eks.get("tenants") or []
        keys = platform_mounted_keys(mounted_secret_keys(), eks["secret_arns"], tenants)
        unset = unset_secrets(keys, eks["secret_arns"], secretsmanager)
        unset += unset_tenant_secrets(tenants, secretsmanager)
        for key in unset:
            print(f"{LOG} {unset_message(key)}")
        command = command + [part for key in unset for part in ("--omit-secret", key)]
    exit_code = run_deployer(
        session.client("ecs"), session.client("logs"), eks["deployer"], command
    )
    if hosts:
        balancers = gateway_load_balancers(
            session.client("elbv2"), eks["cluster_name"], eks["vpc_id"], hosts
        )
        publish_gateway_hosts(
            session.client("route53"), eks["mcp_gateway_zone_id"], hosts, balancers
        )
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default="")
    parser.add_argument(
        "command",
        nargs="*",
        default=["deploy"],
        help=(
            "deploy, check-policies, or restart DEPLOYMENT... "
            "(namespace/name, tenant-workers, tenant-review-workers, or mcp-gateway)"
        ),
    )
    args = parser.parse_args(argv)

    from scripts.aws_clients import configure_aws_fips
    from scripts.config import get_settings

    settings = get_settings()
    configure_aws_fips(settings)
    import boto3

    session = boto3.Session(region_name=args.region or settings.aws_region)
    try:
        return deploy(session, _terraform_eks(dict(os.environ)), args.command or ["deploy"])
    except DeployError as exc:
        print(f"{LOG} {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
