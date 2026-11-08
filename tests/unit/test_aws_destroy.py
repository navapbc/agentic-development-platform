from __future__ import annotations

import json
import subprocess

from scripts import aws_destroy

REPOSITORY = 'module.container_services.module.repository["platform"]'


def _state() -> dict:
    return {
        "values": {
            "root_module": {
                "resources": [
                    {
                        "mode": "managed",
                        "address": "aws_vpc.this[0]",
                        "type": "aws_vpc",
                        "values": {"id": "vpc-1"},
                    },
                    {
                        "mode": "data",
                        "address": "data.aws_region.current",
                        "type": "aws_region",
                        "values": {},
                    },
                ],
                "child_modules": [
                    {
                        "address": "module.container_services",
                        "resources": [
                            {
                                "mode": "managed",
                                "address": "module.container_services.aws_ecs_cluster.this[0]",
                                "type": "aws_ecs_cluster",
                                "values": {"arn": "arn:aws:ecs:us-east-1:1:cluster/c"},
                            },
                            {
                                "mode": "managed",
                                "address": "module.container_services.aws_s3_bucket.lb_logs[0]",
                                "type": "aws_s3_bucket",
                                "values": {"bucket": "logs", "force_destroy": True},
                            },
                        ],
                        "child_modules": [
                            {
                                "address": REPOSITORY,
                                "resources": [
                                    {
                                        "mode": "managed",
                                        "address": f"{REPOSITORY}.aws_ecr_repository.this[0]",
                                        "type": "aws_ecr_repository",
                                        "values": {"name": "agentic-platform/platform"},
                                    },
                                    {
                                        "mode": "managed",
                                        "address": f"{REPOSITORY}.aws_kms_key.this[0]",
                                        "type": "aws_kms_key",
                                        "values": {},
                                    },
                                ],
                            }
                        ],
                    },
                    {
                        "address": "module.rebar_ticket_store",
                        "resources": [
                            {
                                "mode": "managed",
                                "address": "module.rebar_ticket_store.aws_s3_bucket.this[0]",
                                "type": "aws_s3_bucket",
                                "values": {"bucket": "tickets", "force_destroy": False},
                            }
                        ],
                    },
                ],
            }
        }
    }


def test_the_image_modules_are_kept_whole_and_everything_else_is_targeted() -> None:
    teardown = aws_destroy.plan_teardown(
        aws_destroy.managed_resources(_state()), name_prefix="agentic-platform", delete_images=False
    )

    assert teardown.kept == [
        f"{REPOSITORY}.aws_ecr_repository.this[0]",
        f"{REPOSITORY}.aws_kms_key.this[0]",
    ]
    assert teardown.targets == [
        "aws_vpc.this[0]",
        "module.container_services.aws_ecs_cluster.this[0]",
        "module.container_services.aws_s3_bucket.lb_logs[0]",
        "module.rebar_ticket_store.aws_s3_bucket.this[0]",
    ]
    assert teardown.repositories == ["agentic-platform/platform"]
    assert teardown.buckets == ["tickets"]
    assert teardown.vpc_ids == ["vpc-1"]
    assert teardown.clusters == ["arn:aws:ecs:us-east-1:1:cluster/c"]


def test_delete_images_targets_the_repositories_too() -> None:
    teardown = aws_destroy.plan_teardown(
        aws_destroy.managed_resources(_state()), name_prefix="agentic-platform", delete_images=True
    )

    assert teardown.kept == []
    assert any("aws_ecr_repository" in address for address in teardown.targets)


def test_the_name_prefix_comes_from_the_var_file() -> None:
    assert aws_destroy.name_prefix_of('region = "us-east-1"\nname_prefix = "sbx"\n') == "sbx"
    assert aws_destroy.name_prefix_of('region = "us-east-1"\n') == "agentic-platform"


class _Aws:
    """Answer aws CLI calls from canned responses and record each call."""

    def __init__(self, responses: dict[tuple[str, ...], list[dict]]) -> None:
        self.responses = {key: list(values) for key, values in responses.items()}
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        args = argv[1:-2]
        self.calls.append(args)
        for prefix, values in self.responses.items():
            if tuple(args[: len(prefix)]) == prefix and values:
                return subprocess.CompletedProcess(argv, 0, json.dumps(values.pop(0)), "")
        return subprocess.CompletedProcess(argv, 0, "{}", "")


def test_only_the_controller_load_balancers_in_the_vpc_are_removed() -> None:
    aws = _Aws(
        {
            ("elbv2", "describe-load-balancers"): [
                {
                    "LoadBalancers": [
                        {
                            "LoadBalancerArn": "arn:k8s",
                            "LoadBalancerName": "k8s-web",
                            "VpcId": "vpc-1",
                        },
                        {
                            "LoadBalancerArn": "arn:tf",
                            "LoadBalancerName": "internal",
                            "VpcId": "vpc-1",
                        },
                        {"LoadBalancerArn": "arn:other", "LoadBalancerName": "x", "VpcId": "vpc-9"},
                    ]
                }
            ],
            ("elbv2", "describe-tags"): [
                {
                    "TagDescriptions": [
                        {
                            "ResourceArn": "arn:k8s",
                            "Tags": [{"Key": "elbv2.k8s.aws/cluster", "Value": "c"}],
                        },
                        {"ResourceArn": "arn:tf", "Tags": [{"Key": "Name", "Value": "internal"}]},
                    ]
                }
            ],
        }
    )

    removed = aws_destroy.remove_controller_resources(["vpc-1"], aws)

    assert removed == ["k8s-web"]
    deletes = [call for call in aws.calls if call[:2] == ["elbv2", "delete-load-balancer"]]
    assert deletes == [["elbv2", "delete-load-balancer", "--load-balancer-arn", "arn:k8s"]]


def test_a_task_outside_a_service_is_stopped_and_a_service_task_is_left() -> None:
    aws = _Aws(
        {
            ("ecs", "list-tasks"): [{"taskArns": ["arn:loose", "arn:svc"]}],
            ("ecs", "describe-tasks"): [
                {"tasks": [{"group": "family:jumphost"}]},
                {"tasks": [{"group": "service:coordination"}]},
            ],
        }
    )

    assert aws_destroy.stop_loose_tasks(["arn:cluster"], aws) == ["arn:loose"]


def test_a_versioned_bucket_is_emptied_of_versions_and_delete_markers() -> None:
    aws = _Aws(
        {
            ("s3api", "list-object-versions"): [
                {
                    "Versions": [{"Key": "a", "VersionId": "1"}],
                    "DeleteMarkers": [{"Key": "a", "VersionId": "2"}],
                },
                {},
            ]
        }
    )

    assert aws_destroy.empty_bucket("tickets", aws) == 2
    deletion = next(call for call in aws.calls if call[:2] == ["s3api", "delete-objects"])
    assert json.loads(deletion[-1])["Objects"] == [
        {"Key": "a", "VersionId": "1"},
        {"Key": "a", "VersionId": "2"},
    ]


def test_only_secrets_pending_deletion_are_purged() -> None:
    aws = _Aws(
        {
            ("secretsmanager", "list-secrets"): [
                {
                    "SecretList": [
                        {
                            "ARN": "arn:gone",
                            "Name": "agentic-platform/generated/x",
                            "DeletedDate": 1,
                        },
                        {"ARN": "arn:live", "Name": "agentic-platform/generated/y"},
                    ]
                }
            ]
        }
    )

    assert aws_destroy.purge_pending_secrets("agentic-platform/", aws) == [
        "agentic-platform/generated/x"
    ]
