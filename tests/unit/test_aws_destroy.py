from __future__ import annotations

import json
import subprocess
import types

import pytest
from scripts import aws_destroy

REPOSITORY = 'module.container_services.module.repository["platform"]'
STORE = 'module.tenant["adp"].module.ticket_store'


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
                        "address": 'module.tenant["adp"]',
                        "resources": [
                            {
                                "mode": "managed",
                                "address": 'module.tenant["adp"].aws_iam_policy.worker[0]',
                                "type": "aws_iam_policy",
                                "values": {},
                                "depends_on": [
                                    "module.tenant.module.ticket_store.aws_iam_role.reader"
                                ],
                            }
                        ],
                        "child_modules": [
                            {
                                "address": STORE,
                                "resources": [
                                    {
                                        "mode": "managed",
                                        "address": f"{STORE}.aws_s3_bucket.this[0]",
                                        "type": "aws_s3_bucket",
                                        "values": {"bucket": "tickets", "force_destroy": False},
                                        "depends_on": [
                                            "module.tenant.data.aws_caller_identity.current"
                                        ],
                                    },
                                    {
                                        "mode": "managed",
                                        "address": f"{STORE}.aws_iam_role.reader[0]",
                                        "type": "aws_iam_role",
                                        "values": {},
                                    },
                                    {
                                        "mode": "managed",
                                        "address": f"{STORE}.aws_kms_key.this[0]",
                                        "type": "aws_kms_key",
                                        "values": {},
                                        "depends_on": [
                                            "module.tenant.module.ticket_store.aws_iam_role.reader"
                                        ],
                                    },
                                ],
                            }
                        ],
                    },
                ],
            }
        }
    }


def test_the_image_and_ticket_store_modules_are_kept_whole_and_everything_else_is_targeted() -> (
    None
):
    teardown = aws_destroy.plan_teardown(
        aws_destroy.managed_resources(_state()), name_prefix="agentic-platform", delete_images=False
    )

    assert teardown.kept == [
        f"{REPOSITORY}.aws_ecr_repository.this[0]",
        f"{REPOSITORY}.aws_kms_key.this[0]",
        f"{STORE}.aws_s3_bucket.this[0]",
        f"{STORE}.aws_iam_role.reader[0]",
        f"{STORE}.aws_kms_key.this[0]",
    ]
    assert teardown.targets == [
        "aws_vpc.this[0]",
        "module.container_services.aws_ecs_cluster.this[0]",
        "module.container_services.aws_s3_bucket.lb_logs[0]",
        'module.tenant["adp"].aws_iam_policy.worker[0]',
    ]
    assert teardown.repositories == ["agentic-platform/platform"]
    assert teardown.stores == ["tickets"]
    assert teardown.released == []
    assert teardown.buckets == []
    assert teardown.vpc_ids == ["vpc-1"]
    assert teardown.clusters == ["arn:aws:ecs:us-east-1:1:cluster/c"]


def test_delete_images_targets_the_repositories_too() -> None:
    teardown = aws_destroy.plan_teardown(
        aws_destroy.managed_resources(_state()), name_prefix="agentic-platform", delete_images=True
    )

    assert not any(address.startswith(REPOSITORY) for address in teardown.kept)
    assert any("aws_ecr_repository" in address for address in teardown.targets)


def test_delete_stores_targets_the_store_and_releases_its_bucket_from_the_plan() -> None:
    teardown = aws_destroy.plan_teardown(
        aws_destroy.managed_resources(_state()),
        name_prefix="agentic-platform",
        delete_images=False,
        delete_stores=True,
    )

    assert f"{STORE}.aws_kms_key.this[0]" in teardown.targets
    assert f"{STORE}.aws_iam_role.reader[0]" in teardown.targets
    assert f"{STORE}.aws_s3_bucket.this[0]" not in teardown.targets
    assert [bucket.address for bucket in teardown.released] == [f"{STORE}.aws_s3_bucket.this[0]"]
    assert teardown.buckets == []


def test_a_kept_store_that_depends_on_a_destroyed_resource_is_refused() -> None:
    state = _state()
    state["values"]["root_module"]["resources"].append(
        {
            "mode": "managed",
            "address": 'module.eks_cluster.aws_iam_role.pod_identity["tenant_adp"]',
            "type": "aws_iam_role",
            "values": {},
        }
    )
    store = state["values"]["root_module"]["child_modules"][1]["child_modules"][0]
    store["resources"][1]["depends_on"] = ["module.eks_cluster.aws_iam_role.pod_identity"]

    with pytest.raises(aws_destroy.DestroyError, match=r"pod_identity\["):
        aws_destroy.plan_teardown(
            aws_destroy.managed_resources(state),
            name_prefix="agentic-platform",
            delete_images=False,
        )


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


class _Account:
    """Answer the terraform and aws CLI calls of main() from a small model of state and S3."""

    def __init__(self, resources: list[dict], buckets: dict[str, int]) -> None:
        self.resources = resources
        self.buckets = dict(buckets)
        self.planned: list[str] = []
        self.racing_writes = 0
        self.refused: set[str] = set()
        self.calls: list[list[str]] = []

    def state(self) -> dict:
        store = [r for r in self.resources if r["address"].startswith(STORE)]
        return {
            "values": {
                "root_module": {
                    "resources": [r for r in self.resources if r not in store],
                    "child_modules": [{"address": STORE, "resources": store}],
                }
            }
        }

    def _fail(self, argv: list[str], stderr: str, check: bool) -> subprocess.CompletedProcess:
        if check:
            raise subprocess.CalledProcessError(254, argv, "", stderr)
        return subprocess.CompletedProcess(argv, 254, "", stderr)

    def __call__(self, argv, *, timeout=None, check=False, **options):
        argv = list(argv)
        self.calls.append(argv)
        if argv[0] == "terraform":
            return self._terraform(argv, argv[2:])
        command = argv[1:-2]
        if command[:2] == ["sts", "get-caller-identity"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"Account": "111111111111"}))
        if command[:1] == ["s3api"]:
            bucket = command[command.index("--bucket") + 1]
            if bucket not in self.buckets:
                message = f"An error occurred (NoSuchBucket) when calling {command[1]}"
                return self._fail(argv, message, check)
            if command[1] == "list-object-versions":
                versions = [{"Key": "k", "VersionId": str(n)} for n in range(self.buckets[bucket])]
                return subprocess.CompletedProcess(argv, 0, json.dumps({"Versions": versions}))
            if command[1] == "delete-objects":
                objects = json.loads(command[-1])["Objects"]
                if bucket in self.refused:
                    refused_calls = sum(c[1:3] == ["s3api", "delete-objects"] for c in self.calls)
                    if refused_calls > 3:
                        raise AssertionError("a refused object delete was retried")
                    errors = [
                        {"Key": o["Key"], "Code": "AccessDenied", "Message": "Access Denied"}
                        for o in objects
                    ]
                    return subprocess.CompletedProcess(argv, 0, json.dumps({"Errors": errors}))
                self.buckets[bucket] -= len(objects)
            if command[1] == "delete-bucket":
                if self.racing_writes:
                    self.racing_writes -= 1
                    self.buckets[bucket] += 1
                if self.buckets[bucket]:
                    message = "An error occurred (BucketNotEmpty) when calling DeleteBucket"
                    return self._fail(argv, message, check)
                del self.buckets[bucket]
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    def _terraform(self, argv: list[str], command: list[str]) -> subprocess.CompletedProcess:
        if command[:1] == ["show"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.state()))
        if command[:2] == ["plan", "-destroy"]:
            self.planned = [a.removeprefix("-target=") for a in command if a.startswith("-target=")]
        elif command[:1] == ["apply"]:
            self.resources = [r for r in self.resources if r["address"] not in self.planned]
            self.buckets = {name: count + 2 for name, count in self.buckets.items()}
        elif command[:2] == ["state", "rm"]:
            self.resources = [r for r in self.resources if r["address"] != command[2]]
        return subprocess.CompletedProcess(argv, 0, "", "")

    def ran(self, *prefix: str) -> bool:
        return any(call[: len(prefix)] == list(prefix) for call in self.calls)


STORE_BUCKET = f"{STORE}.aws_s3_bucket.this[0]"


def _store_resources() -> list[dict]:
    return [
        {
            "mode": "managed",
            "address": "aws_vpc.this[0]",
            "type": "aws_vpc",
            "values": {"id": "vpc-1"},
        },
        {
            "mode": "managed",
            "address": f"{STORE}.aws_s3_bucket_policy.this[0]",
            "type": "aws_s3_bucket_policy",
            "values": {},
        },
        {
            "mode": "managed",
            "address": STORE_BUCKET,
            "type": "aws_s3_bucket",
            "values": {"bucket": "tickets", "force_destroy": False},
        },
    ]


@pytest.fixture
def account(monkeypatch, tmp_path):
    def install(resources: list[dict], buckets: dict[str, int]) -> _Account:
        fake = _Account(resources, buckets)
        (tmp_path / "greenfield.tfvars").write_text('name_prefix = "agentic-platform"\n')
        monkeypatch.setattr(aws_destroy, "ROOT", tmp_path)
        monkeypatch.setattr(aws_destroy.process, "run", fake)
        monkeypatch.setattr("builtins.input", lambda prompt: "111111111111")
        monkeypatch.setattr(
            "scripts.config.get_settings", lambda: types.SimpleNamespace(aws_region="us-east-1")
        )
        return fake

    return install


def test_a_failed_bucket_delete_reports_the_rerun_and_the_rerun_finishes_it(
    account, capsys
) -> None:
    fake = account(_store_resources(), {"tickets": 3})
    fake.racing_writes = 1

    assert aws_destroy.main(["--delete-stores"]) == 1

    err = capsys.readouterr().err
    assert "tickets was not deleted and stays in state" in err
    assert "BucketNotEmpty" in err
    assert "inv aws-destroy --var-file greenfield.tfvars --delete-stores" in err
    assert "Traceback" not in err
    assert fake.ran("aws", "secretsmanager", "list-secrets")
    assert [r["address"] for r in fake.resources] == [STORE_BUCKET]

    fake.calls.clear()
    assert aws_destroy.main(["--delete-stores"]) == 0

    assert not fake.ran("terraform", f"-chdir={aws_destroy.ROOT}", "plan")
    assert "tickets" not in fake.buckets
    assert fake.resources == []


def test_a_rerun_with_only_the_released_bucket_empties_deletes_and_forgets_it(account) -> None:
    fake = account([_store_resources()[2]], {"tickets": 2})

    assert aws_destroy.main(["--delete-stores"]) == 0

    assert fake.ran("aws", "s3api", "delete-objects")
    assert not fake.ran("terraform", f"-chdir={aws_destroy.ROOT}", "plan")
    assert "tickets" not in fake.buckets
    assert fake.resources == []


def test_a_refused_object_delete_fails_the_bucket_instead_of_retrying_forever(
    account, capsys
) -> None:
    fake = account([_store_resources()[2]], {"tickets": 2})
    fake.refused.add("tickets")

    assert aws_destroy.main(["--delete-stores"]) == 1

    err = capsys.readouterr().err
    assert "tickets was not deleted and stays in state" in err
    assert "AccessDenied" in err
    assert "Traceback" not in err
    assert [r["address"] for r in fake.resources] == [STORE_BUCKET]
    assert sum(call[1:3] == ["s3api", "delete-objects"] for call in fake.calls) == 1


def test_a_bucket_already_deleted_is_removed_from_state(account) -> None:
    fake = account([_store_resources()[2]], {})

    assert aws_destroy.main(["--delete-stores"]) == 0

    assert fake.ran("terraform", f"-chdir={aws_destroy.ROOT}", "state", "rm", STORE_BUCKET)
    assert fake.resources == []
