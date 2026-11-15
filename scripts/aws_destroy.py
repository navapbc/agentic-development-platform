"""Destroy the private-bedrock composition root and keep the bootstrap root and the images.

The bootstrap root, which holds the state and access-log buckets, is a separate root and is never
touched. Every ECR repository the composition root manages is kept with its key and images, so a
later apply finds the published images in place, unless --delete-images is given. The run removes
first what Terraform does not own but a destroy trips over: the load balancers, target groups,
and security groups the AWS Load Balancer Controller creates for Ingress resources, ECS tasks
started outside a service such as a jumphost session, and the objects in buckets Terraform would
not empty. It plans the destroy, applies it only after the operator types the account ID, then
removes the secrets left pending deletion so a rebuild can reuse their names, and reports what
still carries the name prefix.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from services import process

from scripts.publish_platform_images import IMAGES_FILE

LOG = "[aws-destroy]"
ROOT = IMAGES_FILE.parent
PLAN = "destroy.tfplan"
CONTROLLER_TAG = "elbv2.k8s.aws/cluster"

Runner = Callable[..., process.CompletedProcess[Any]]


class DestroyError(Exception):
    """The teardown cannot proceed."""


@dataclass(frozen=True)
class Resource:
    address: str
    module: str
    type: str
    values: Mapping[str, Any]


@dataclass
class Teardown:
    """What a destroy removes, what it keeps, and what has to be cleared first."""

    targets: list[str]
    kept: list[str]
    repositories: list[str]
    buckets: list[str]
    clusters: list[str]
    vpc_ids: list[str]
    secret_prefix: str
    notes: list[str] = field(default_factory=list)


def name_prefix_of(var_file_text: str, default: str = "agentic-platform") -> str:
    match = re.search(r'^\s*name_prefix\s*=\s*"([^"]+)"', var_file_text, re.MULTILINE)
    return match.group(1) if match else default


def managed_resources(state: Mapping[str, Any]) -> list[Resource]:
    """Flatten terraform show -json into the managed resources of every module."""
    found: list[Resource] = []

    def walk(module: Mapping[str, Any]) -> None:
        for resource in module.get("resources", []):
            if resource.get("mode") == "managed":
                found.append(
                    Resource(
                        address=resource["address"],
                        module=module.get("address", ""),
                        type=resource["type"],
                        values=resource.get("values") or {},
                    )
                )
        for child in module.get("child_modules", []):
            walk(child)

    walk(state.get("values", {}).get("root_module", {}))
    return found


def plan_teardown(
    resources: Iterable[Resource], *, name_prefix: str, delete_images: bool
) -> Teardown:
    """Split the managed resources into the destroy targets and the image modules kept.

    A module instance that holds an ECR repository is kept whole, so the repository's key and
    lifecycle policy stay with it.
    """
    resources = list(resources)
    repository_modules = {r.module for r in resources if r.type == "aws_ecr_repository"}
    kept_modules = set() if delete_images else repository_modules
    kept = [r.address for r in resources if r.module in kept_modules]
    return Teardown(
        targets=[r.address for r in resources if r.module not in kept_modules],
        kept=kept,
        repositories=[
            r.values.get("name", "") for r in resources if r.type == "aws_ecr_repository"
        ],
        buckets=[
            r.values["bucket"]
            for r in resources
            if r.type == "aws_s3_bucket" and not r.values.get("force_destroy")
        ],
        clusters=[r.values["arn"] for r in resources if r.type == "aws_ecs_cluster"],
        vpc_ids=[r.values["id"] for r in resources if r.type == "aws_vpc"],
        secret_prefix=f"{name_prefix}/",
    )


def _aws(argv: list[str], run: Runner, *, check: bool = True) -> Any:
    result = run(
        ["aws", *argv, "--output", "json"],
        timeout=process.MEDIUM_COMMAND_TIMEOUT_SECONDS,
        capture_output=True,
        text=True,
        check=check,
    )
    return json.loads(result.stdout) if result.stdout.strip() else {}


def controller_load_balancers(vpc_ids: list[str], run: Runner) -> list[dict[str, Any]]:
    """Return the load balancers in the composition's VPCs that the controller created."""
    balancers = _aws(["elbv2", "describe-load-balancers"], run).get("LoadBalancers", [])
    candidates = [lb for lb in balancers if lb.get("VpcId") in vpc_ids]
    if not candidates:
        return []
    tagged = []
    for start in range(0, len(candidates), 20):
        chunk = candidates[start : start + 20]
        descriptions = _aws(
            ["elbv2", "describe-tags", "--resource-arns", *[lb["LoadBalancerArn"] for lb in chunk]],
            run,
        ).get("TagDescriptions", [])
        owned = {
            d["ResourceArn"]
            for d in descriptions
            if any(tag["Key"] == CONTROLLER_TAG for tag in d.get("Tags", []))
        }
        tagged += [lb for lb in chunk if lb["LoadBalancerArn"] in owned]
    return tagged


def remove_controller_resources(vpc_ids: list[str], run: Runner) -> list[str]:
    """Delete the controller's load balancers, then its target groups and security groups."""
    removed = []
    balancers = controller_load_balancers(vpc_ids, run)
    for lb in balancers:
        _aws(["elbv2", "delete-load-balancer", "--load-balancer-arn", lb["LoadBalancerArn"]], run)
        removed.append(lb["LoadBalancerName"])
    if balancers:
        _aws(
            [
                "elbv2",
                "wait",
                "load-balancers-deleted",
                "--load-balancer-arns",
                *[lb["LoadBalancerArn"] for lb in balancers],
            ],
            run,
        )
    groups = _aws(["elbv2", "describe-target-groups"], run).get("TargetGroups", [])
    for group in groups:
        if group.get("VpcId") not in vpc_ids:
            continue
        tags = _aws(
            ["elbv2", "describe-tags", "--resource-arns", group["TargetGroupArn"]], run
        ).get("TagDescriptions", [{}])[0]
        if any(tag["Key"] == CONTROLLER_TAG for tag in tags.get("Tags", [])):
            _aws(
                ["elbv2", "delete-target-group", "--target-group-arn", group["TargetGroupArn"]], run
            )
            removed.append(group["TargetGroupName"])
    for vpc_id in vpc_ids:
        security_groups = _aws(
            [
                "ec2",
                "describe-security-groups",
                "--filters",
                f"Name=vpc-id,Values={vpc_id}",
                f"Name=tag-key,Values={CONTROLLER_TAG}",
            ],
            run,
        ).get("SecurityGroups", [])
        for group in security_groups:
            _aws(["ec2", "delete-security-group", "--group-id", group["GroupId"]], run)
            removed.append(group["GroupId"])
    return removed


def stop_loose_tasks(clusters: list[str], run: Runner) -> list[str]:
    """Stop the tasks a service does not own, such as a jumphost session, which block a cluster."""
    stopped = []
    for cluster in clusters:
        for arn in _aws(["ecs", "list-tasks", "--cluster", cluster], run).get("taskArns", []):
            task = _aws(["ecs", "describe-tasks", "--cluster", cluster, "--tasks", arn], run)
            group = (task.get("tasks") or [{}])[0].get("group", "")
            if not group.startswith("service:"):
                _aws(["ecs", "stop-task", "--cluster", cluster, "--task", arn], run)
                stopped.append(arn)
    return stopped


def empty_bucket(bucket: str, run: Runner) -> int:
    """Delete every object version and delete marker, which a versioned bucket keeps."""
    deleted = 0
    while True:
        listing = _aws(
            ["s3api", "list-object-versions", "--bucket", bucket, "--max-items", "1000"], run
        )
        keys = [
            {"Key": entry["Key"], "VersionId": entry["VersionId"]}
            for entry in listing.get("Versions", []) + listing.get("DeleteMarkers", [])
        ]
        if not keys:
            return deleted
        _aws(
            [
                "s3api",
                "delete-objects",
                "--bucket",
                bucket,
                "--delete",
                json.dumps({"Objects": keys, "Quiet": True}),
            ],
            run,
        )
        deleted += len(keys)


def delete_images(repositories: list[str], run: Runner) -> int:
    deleted = 0
    for name in repositories:
        images = _aws(["ecr", "list-images", "--repository-name", name], run).get("imageIds", [])
        for start in range(0, len(images), 100):
            chunk = images[start : start + 100]
            _aws(
                [
                    "ecr",
                    "batch-delete-image",
                    "--repository-name",
                    name,
                    "--image-ids",
                    json.dumps(chunk),
                ],
                run,
            )
            deleted += len(chunk)
    return deleted


def purge_pending_secrets(prefix: str, run: Runner) -> list[str]:
    """Delete the secrets left pending deletion, whose names a rebuild would otherwise refuse."""
    secrets = _aws(
        [
            "secretsmanager",
            "list-secrets",
            "--include-planned-deletion",
            "--filters",
            f"Key=name,Values={prefix}",
        ],
        run,
    ).get("SecretList", [])
    purged = []
    for secret in secrets:
        if secret.get("DeletedDate"):
            _aws(
                [
                    "secretsmanager",
                    "delete-secret",
                    "--secret-id",
                    secret["ARN"],
                    "--force-delete-without-recovery",
                ],
                run,
            )
            purged.append(secret["Name"])
    return purged


def remaining(name_prefix: str, run: Runner) -> list[str]:
    resources = _aws(["resourcegroupstaggingapi", "get-resources"], run).get(
        "ResourceTagMappingList", []
    )
    return sorted(r["ResourceARN"] for r in resources if name_prefix in r["ResourceARN"])


def terraform(argv: list[str], run: Runner, **options: Any) -> process.CompletedProcess[Any]:
    return run(
        ["terraform", f"-chdir={ROOT}", *argv],
        timeout=process.LONG_COMMAND_TIMEOUT_SECONDS,
        **options,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--var-file", default="greenfield.tfvars", help="In the composition root.")
    parser.add_argument(
        "--delete-images", action="store_true", help="Also delete the ECR repositories and images."
    )
    parser.add_argument("--plan-only", action="store_true", help="Show the destroy and stop.")
    args = parser.parse_args(argv)
    run: Runner = process.run

    from scripts.config import get_settings

    name_prefix = name_prefix_of((ROOT / args.var_file).read_text(encoding="utf-8"))
    shown = terraform(["show", "-json"], run, capture_output=True, text=True, check=True)
    state = json.loads(shown.stdout)
    teardown = plan_teardown(
        managed_resources(state), name_prefix=name_prefix, delete_images=args.delete_images
    )
    if not teardown.targets:
        print(f"{LOG} the composition root manages nothing to destroy")
        return 0
    account = _aws(["sts", "get-caller-identity"], run)["Account"]
    region = get_settings().aws_region

    planned = terraform(
        [
            "plan",
            "-destroy",
            "-input=false",
            "-compact-warnings",
            f"-var-file={args.var_file}",
            f"-out={PLAN}",
            *[f"-target={address}" for address in teardown.targets],
        ],
        run,
    )
    if planned.returncode != 0:
        print(f"{LOG} terraform plan -destroy exited {planned.returncode}", file=sys.stderr)
        return 1
    print(f"{LOG} account {account} in {region}")
    print(f"{LOG} destroys {len(teardown.targets)} resources and keeps {len(teardown.kept)}")
    if not args.delete_images:
        print(f"{LOG} keeps the repositories {', '.join(sorted(teardown.repositories))}")
    if teardown.buckets:
        print(f"{LOG} empties the buckets {', '.join(teardown.buckets)} before the destroy")
    if args.plan_only:
        (ROOT / PLAN).unlink(missing_ok=True)
        return 0
    typed = input(f"{LOG} type the account ID {account} to destroy: ").strip()
    if typed != account:
        (ROOT / PLAN).unlink(missing_ok=True)
        print(f"{LOG} not destroyed")
        return 1
    try:
        for name in remove_controller_resources(teardown.vpc_ids, run):
            print(f"{LOG} removed the Ingress resource {name}")
        for arn in stop_loose_tasks(teardown.clusters, run):
            print(f"{LOG} stopped the task {arn}")
        for bucket in teardown.buckets:
            print(f"{LOG} emptied {bucket}, {empty_bucket(bucket, run)} object versions")
        if args.delete_images:
            print(f"{LOG} deleted {delete_images(teardown.repositories, run)} images")
        terraform(["apply", "-input=false", PLAN], run, check=True)
    finally:
        (ROOT / PLAN).unlink(missing_ok=True)
    for name in purge_pending_secrets(teardown.secret_prefix, run):
        print(f"{LOG} removed the pending secret {name}")
    left = remaining(name_prefix, run)
    print(
        f"{LOG} {len(left)} tagged resources still carry {name_prefix}. The tagging API lists a "
        "deleted resource, such as an inactive ECS cluster or task definition, for a while after "
        "the delete, and the kept repositories and the bootstrap buckets stay."
    )
    for arn in left:
        print(f"{LOG}   {arn}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
