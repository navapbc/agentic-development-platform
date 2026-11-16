"""Destroy the private-bedrock composition root and keep the bootstrap root, images, and tickets.

The bootstrap root, which holds the state and access-log buckets, is a separate root and is never
touched. Every ECR repository the composition root manages is kept with its key and images, so a
later apply finds the published images in place, unless --delete-images is given. Every rebar
ticket store is kept with its bucket, key, and roles, because it is the system of record for a
tenant's tickets, unless --delete-stores is given. Its bucket carries prevent_destroy, so with
--delete-stores the bucket stays out of the planned destroy, and after the destroy the run empties
and deletes the bucket and removes it from state. A rerun with --delete-stores finishes a bucket a
failed delete left in state. The run removes
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
import shlex
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
TICKET_STORE_MODULE = re.compile(r"(?:^|\.)module\.(?:rebar_)?ticket_store(?:\[[^\]]*\])?$")
INSTANCE_KEY = re.compile(r'\[(?:"(?:[^"\\]|\\.)*"|\d+)\]')

Runner = Callable[..., process.CompletedProcess[Any]]


class DestroyError(Exception):
    """The teardown cannot proceed."""


@dataclass(frozen=True)
class Resource:
    address: str
    module: str
    type: str
    values: Mapping[str, Any]
    depends_on: tuple[str, ...] = ()

    @property
    def config_address(self) -> str:
        """The address without instance keys, the form Terraform records dependencies in."""
        return INSTANCE_KEY.sub("", self.address)


@dataclass
class Teardown:
    """What a destroy removes, what it keeps, and what has to be cleared first."""

    targets: list[str]
    kept: list[str]
    repositories: list[str]
    stores: list[str]
    released: list[Resource]
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
                        depends_on=tuple(resource.get("depends_on", ())),
                    )
                )
        for child in module.get("child_modules", []):
            walk(child)

    walk(state.get("values", {}).get("root_module", {}))
    return found


def dependencies_of(kept: Iterable[Resource], resources: Iterable[Resource]) -> list[Resource]:
    """Return every resource the kept ones depend on, directly or through another."""
    by_config: dict[str, list[Resource]] = {}
    for resource in resources:
        by_config.setdefault(resource.config_address, []).append(resource)
    found = {resource.address: resource for resource in kept}
    pending = list(found.values())
    while pending:
        for dependency in pending.pop().depends_on:
            for resource in by_config.get(dependency, []):
                if resource.address not in found:
                    found[resource.address] = resource
                    pending.append(resource)
    return list(found.values())


def plan_teardown(
    resources: Iterable[Resource],
    *,
    name_prefix: str,
    delete_images: bool,
    delete_stores: bool = False,
) -> Teardown:
    """Split the managed resources into the destroy targets and the modules kept.

    A module instance that holds an ECR repository, or a rebar ticket store module instance, is
    kept whole, so the repository's key and lifecycle policy, and the store's bucket, key, and
    roles, stay with it. A targeted destroy also destroys whatever depends on a target, so a kept
    module that depends on a resource outside the kept modules is refused. With delete_stores,
    each store bucket is released instead of targeted, because prevent_destroy refuses a plan that
    destroys it.
    """
    resources = list(resources)
    repository_modules = {r.module for r in resources if r.type == "aws_ecr_repository"}
    store_modules = {r.module for r in resources if TICKET_STORE_MODULE.search(r.module)}
    kept_modules = (set() if delete_images else repository_modules) | (
        set() if delete_stores else store_modules
    )
    kept = [r for r in resources if r.module in kept_modules]
    outside = [r.address for r in dependencies_of(kept, resources) if r.module not in kept_modules]
    if outside:
        raise DestroyError(
            f"the kept modules depend on {', '.join(outside)}, and a destroy of those would "
            "remove the kept modules with them. Apply the composition root so the state records "
            "the current dependencies, then run the destroy again."
        )
    kept_addresses = {r.address for r in kept}
    store_buckets = [
        r for r in resources if r.module in store_modules and r.type == "aws_s3_bucket"
    ]
    released = store_buckets if delete_stores else []
    released_addresses = {r.address for r in released}
    return Teardown(
        targets=[
            r.address
            for r in resources
            if r.address not in kept_addresses and r.address not in released_addresses
        ],
        kept=[r.address for r in kept],
        repositories=[
            r.values.get("name", "") for r in resources if r.type == "aws_ecr_repository"
        ],
        stores=[r.values.get("bucket", "") for r in store_buckets],
        released=released,
        buckets=[
            r.values["bucket"]
            for r in resources
            if r.type == "aws_s3_bucket"
            and not r.values.get("force_destroy")
            and r.address not in kept_addresses
            and r.address not in released_addresses
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


def delete_released_bucket(bucket: Resource, run: Runner) -> int:
    """Empty and delete a released bucket, then remove it from state.

    The bucket is emptied immediately before the delete, because a writer can add object versions
    while the destroy runs. A bucket already gone counts as deleted, so a rerun only removes it
    from state.
    """
    name = bucket.values["bucket"]
    emptied = 0
    try:
        emptied = empty_bucket(name, run)
        _aws(["s3api", "delete-bucket", "--bucket", name], run)
    except process.CalledProcessError as error:
        if "NoSuchBucket" not in str(error.stderr or ""):
            raise
    terraform(["state", "rm", bucket.address], run, capture_output=True, text=True, check=True)
    return emptied


def delete_released_buckets(released: list[Resource], run: Runner) -> dict[str, str]:
    """Delete every released bucket and return the reason each one that could not be deleted."""
    failed: dict[str, str] = {}
    for bucket in released:
        name = bucket.values["bucket"]
        try:
            emptied = delete_released_bucket(bucket, run)
        except (process.CalledProcessError, process.TimeoutExpired) as error:
            detail = str(getattr(error, "stderr", "") or "").strip() or str(error)
            failed[name] = detail.splitlines()[-1]
            continue
        print(f"{LOG} deleted the ticket store bucket {name}, {emptied} object versions")
    return failed


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
    parser.add_argument(
        "--delete-stores",
        action="store_true",
        help="Also delete the rebar ticket stores and the tickets they hold.",
    )
    parser.add_argument("--plan-only", action="store_true", help="Show the destroy and stop.")
    args = parser.parse_args(argv)
    run: Runner = process.run

    from scripts.config import get_settings

    name_prefix = name_prefix_of((ROOT / args.var_file).read_text(encoding="utf-8"))
    shown = terraform(["show", "-json"], run, capture_output=True, text=True, check=True)
    state = json.loads(shown.stdout)
    try:
        teardown = plan_teardown(
            managed_resources(state),
            name_prefix=name_prefix,
            delete_images=args.delete_images,
            delete_stores=args.delete_stores,
        )
    except DestroyError as error:
        print(f"{LOG} {error}", file=sys.stderr)
        return 1
    if not teardown.targets and not teardown.released:
        print(f"{LOG} the composition root manages nothing to destroy")
        return 0
    account = _aws(["sts", "get-caller-identity"], run)["Account"]
    region = get_settings().aws_region

    if teardown.targets:
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
    if teardown.stores and not args.delete_stores:
        print(f"{LOG} keeps the ticket stores {', '.join(sorted(teardown.stores))}")
    if teardown.released:
        print(
            f"{LOG} deletes the ticket store buckets "
            f"{', '.join(sorted(r.values['bucket'] for r in teardown.released))} after "
            "the destroy and removes them from state, because prevent_destroy keeps them out of "
            "the plan"
        )
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
    if teardown.targets:
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
    failed = delete_released_buckets(teardown.released, run)
    for name in purge_pending_secrets(teardown.secret_prefix, run):
        print(f"{LOG} removed the pending secret {name}")
    left = remaining(name_prefix, run)
    print(
        f"{LOG} {len(left)} tagged resources still carry {name_prefix}. The tagging API lists a "
        "deleted resource, such as an inactive ECS cluster or task definition, for a while after "
        "the delete, and the kept repositories, the kept ticket stores, and the bootstrap buckets "
        "stay."
    )
    for arn in left:
        print(f"{LOG}   {arn}")
    if failed:
        rerun = " ".join(
            [
                "inv aws-destroy",
                f"--var-file {shlex.quote(args.var_file)}",
                *(["--delete-images"] if args.delete_images else []),
                "--delete-stores",
            ]
        )
        for name, reason in failed.items():
            print(
                f"{LOG} the ticket store bucket {name} was not deleted and stays in state. "
                f"AWS reported {reason}",
                file=sys.stderr,
            )
        print(f"{LOG} rerun {rerun} to delete what is left", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
