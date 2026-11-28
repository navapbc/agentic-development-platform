from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
import tasks as tasks_module
from invoke.context import Context
from scripts import tenant_verify_aws as verify
from scripts import tenant_verify_slack
from scripts.webhook_eks_render import SLACK_POSTER
from services import process
from services.integration.tenant_claims import CLAIMS_PROTOCOL_KEY
from tests.unit.test_tenant_claims_store import marker_key

ACCOUNT = "123456789012"
REGION = "us-east-1"
BUCKET = "agentic-platform-tenant-config"
CONFIG_KEY = f"arn:aws:kms:{REGION}:{ACCOUNT}:key/config"
CLUSTER = "agentic-platform"
TASK_ROLES = {"repo-intelligence": "ri-task", "repo-index-worker": "index-task"}
RI_EXECUTION = "ri-execution"
REPOSITORIES = {"adp": "agentic-demo-service", "beta": "beta-service"}
POD_ROLES = {"inbound_webhook": "eks-inbound-webhook", "slack_poster": "eks-slack-poster"}
BUCKET_ARN = f"arn:aws:s3:::{BUCKET}"


def publisher(tenant: str) -> str:
    return role(f"agentic-platform-{tenant}-publisher")


def claims_probe(tenant: str) -> str:
    return role(f"agentic-platform-tenant-{tenant}-claims-probe")


def marker(tenant: str) -> str:
    return f"{BUCKET_ARN}/{marker_key('slack-channel', tenant_verify_slack.SAMPLE_CHANNEL, tenant)}"


def role(name: str) -> str:
    return f"arn:aws:iam::{ACCOUNT}:role/{name}"


def secret(name: str) -> str:
    return f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:{name}-AbCdEf"


def webhook_secret(tenant: str) -> str:
    return secret(f"agentic-platform/tenant/{tenant}/github-webhook-secret")


def worker(tenant: str) -> str:
    return role(f"agentic-platform-tenant-{tenant}")


def reviewer(tenant: str) -> str:
    return role(f"agentic-platform-eks-tenant-{tenant}-review")


def signing_key(tenant: str) -> str:
    return secret(f"agentic-platform/tenants/{tenant}/review-signing-key")


def tenant_output(tenant: str) -> dict[str, Any]:
    token = secret(f"{tenant}/repo-intelligence-token")
    return {
        "secret_arns": {
            "github_token": secret(f"{tenant}/github-token"),
            "repo_intelligence_token": token,
        },
        "repo_intelligence_token_arn": token,
        "kms_key_arn": f"arn:aws:kms:{REGION}:{ACCOUNT}:key/{tenant}",
        "worker_role_arn": worker(tenant),
        "review_role_arn": reviewer(tenant),
        "review_signing_key_arn": signing_key(tenant),
        "review_namespace": f"tenant-{tenant}-review",
        "publisher_role_arn": publisher(tenant),
        "claims_probe_role_arn": claims_probe(tenant),
        "namespace": f"tenant-{tenant}",
        "ticket_store": {"bucket": "tickets", "reader_role_arn": role(f"{tenant}-ticket-reader")},
        "mcp_gateway": {"secret_arns": [secret(f"{tenant}/mcp-gateway")]},
    }


def outputs() -> dict[str, Any]:
    deploy = {
        "cluster": CLUSTER,
        "services": {
            name: {"service": f"svc-{name}", "task_definition": f"td-{name}", "count": 1}
            for name in ("repo-intelligence", "repo-index-worker")
        },
    }
    tenants = {"config_bucket": BUCKET, "tenants": {t: tenant_output(t) for t in REPOSITORIES}}
    return {
        "tenants": {"value": tenants, "type": ["object", {}], "sensitive": False},
        "eks": {
            "value": {
                "region": REGION,
                "pod_identity_role_arns": {key: role(name) for key, name in POD_ROLES.items()},
            },
            "type": ["object", {}],
            "sensitive": False,
        },
        "container_deploy": {"value": deploy, "type": ["object", {}], "sensitive": False},
    }


def deployment() -> verify.Deployment:
    return verify.load_deployment(verify.output_values(outputs()))


def _option(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


def _result(payload: dict[str, Any]) -> str:
    return f"noise\n{verify.RESULT_MARKER}{json.dumps(payload)}\n"


class World:
    """A deployment whose tenant boundary holds, answering the commands the checks run."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.inputs: list[str | None] = []
        self.allowed: set[tuple[str, str, str]] = set()
        self.policies: dict[str, list[str]] = {}
        self.env: dict[tuple[str, str], dict[str, Any]] = {}
        self.statuses: dict[str, dict[str, Any]] = {}
        self.exec_output: dict[str, str] = {}
        self.exec_enabled = True
        self.bucket_policy = {"Statement": [{"Sid": tenant_verify_slack.PROTOCOL_DENY}]}
        self.protocol_object = True
        self.trust_actions = ["sts:AssumeRoleWithWebIdentity"]
        self.poster_ready = 1
        self.poster_tls: dict[str, Any] = {"ready": 200, "days_left": 200}
        self.worker_fingerprints = {tenant: f"{tenant}-fingerprint" for tenant in REPOSITORIES}
        self.poster_fingerprints = dict(self.worker_fingerprints)
        self.review_ready = {tenant: 1 for tenant in REPOSITORIES}
        self.reviews: dict[str, dict[str, Any]] = {}
        bucket_arn = f"arn:aws:s3:::{BUCKET}"
        services = [role(name) for name in TASK_ROLES.values()]
        for tenant in REPOSITORIES:
            record = tenant_output(tenant)
            for arn in record["secret_arns"].values():
                readers = [worker(tenant)]
                if "repo-intelligence-token" in arn:
                    readers.append(role(RI_EXECUTION))
                self.policies[arn] = readers
            self.policies[webhook_secret(tenant)] = [role("inbound-webhook")]
            self.policies[signing_key(tenant)] = [reviewer(tenant)]
            self.policies[record["mcp_gateway"]["secret_arns"][0]] = [role(f"{tenant}-gateway")]
        for arn, readers in self.policies.items():
            self.allowed |= {(verify.GET_VALUE_ACTION, arn, reader) for reader in readers}
        for tenant in REPOSITORIES:
            record = tenant_output(tenant)
            prefix = f"tenants/{tenant}/"
            for principal in (worker(tenant), reviewer(tenant)):
                self.allowed.add(("kms:Decrypt", record["kms_key_arn"], principal))
                self.allowed.add((verify.BUCKET_LIST, f"{bucket_arn}|{prefix}", principal))
                self.allowed.add(("kms:Decrypt", CONFIG_KEY, principal))
            for principal in [worker(tenant), reviewer(tenant), *services]:
                self.allowed.add(
                    (verify.OBJECT_READ, f"{bucket_arn}/{prefix}repositories.yaml", principal)
                )
            self.env[(f"tenant-{tenant}", "activity-worker")] = {
                "names": ["HOME", "PATH", "REPO_INTELLIGENCE_TOKEN", "REPOSITORY_ROOT", "TENANT"],
                "secret_files": ["GITHUB_TOKEN", "REPO_INTELLIGENCE_TOKEN"],
                "values": {"TENANT": tenant, "REPOSITORY_ROOT": f"/tenants/{tenant}/repositories"},
            }
            other = next(o for o in REPOSITORIES if o != tenant)
            self.statuses[tenant] = {
                "tenant": tenant,
                "statuses": {
                    "no_token": "401",
                    "own": "200",
                    "other": {other: [REPOSITORIES[other], "403"]},
                },
                "paths": {
                    f"/tenants/{tenant}": True,
                    f"/tenants/{tenant}/repositories/{REPOSITORIES[tenant]}/.git": True,
                    f"/tenants/{other}": False,
                },
                "bundles": {tenant: "read 3 files", other: "denied"},
            }
            checkout = f"/tenants/{tenant}/repositories/{REPOSITORIES[tenant]}/.git"
            self.exec_output[f"repo-index-worker|ls -1d {checkout}"] = f"\r\n{checkout}\r\n"
            self.env[(f"tenant-{tenant}-review", "review-worker")] = {
                "names": ["HOME", "PATH", "REPOSITORY_ROOT", "REVIEW_SIGNING_KEY", "TENANT"],
                "secret_files": ["REVIEW_SIGNING_KEY", "REGISTRY_TOKEN"],
                "values": {"TENANT": tenant, "REPOSITORY_ROOT": f"/tenants/{tenant}/repositories"},
            }
            self.reviews[tenant] = {
                "tenant": tenant,
                "signing": {
                    "fingerprint": f"SHA256:{tenant}-key",
                    "key_id": f"{tenant}-review-2026",
                    "owners": [tenant],
                },
                "paths": {
                    f"/tenants/{tenant}": True,
                    f"/tenants/{tenant}/repositories/{REPOSITORIES[tenant]}/.git": True,
                    f"/tenants/{other}": False,
                },
                "bundles": {tenant: "read 3 files", other: "denied"},
            }
            self.exec_output[f"repo-index-worker|ls -1 {verify.INDEX_ROOTS[0]}/{tenant}"] = (
                f"{REPOSITORIES[tenant]}@abc123\n"
            )
            self.exec_output[f"repo-index-worker|ls -1 {verify.INDEX_ROOTS[1]}/{tenant}"] = (
                f"ls: cannot access '{verify.INDEX_ROOTS[1]}/{tenant}': No such file or directory\n"
            )
        protocol = f"{BUCKET_ARN}/{CLAIMS_PROTOCOL_KEY}"
        digest_prefix = marker_key("slack-channel", tenant_verify_slack.SAMPLE_CHANNEL, "")
        for tenant in REPOSITORIES:
            upload = f"{BUCKET_ARN}/tenants/{tenant}/escalation.yaml"
            self.allowed |= {
                ("s3:DeleteObject", marker(tenant), publisher(tenant)),
                ("s3:PutObject", f"{upload}|tagged", publisher(tenant)),
                ("s3:PutObjectTagging", f"{upload}|tagged", publisher(tenant)),
                ("s3:GetObject", protocol, publisher(tenant)),
                ("s3:GetObject", protocol, claims_probe(tenant)),
                (verify.BUCKET_LIST, f"{BUCKET_ARN}|{digest_prefix}", claims_probe(tenant)),
            }
        self.allowed |= {("s3:GetObject", protocol, role(name)) for name in POD_ROLES.values()}
        self.env[(verify.PLATFORM_NAMESPACE, "mcp-gateway")] = {
            "names": ["ADP_MCP_TOKEN", "PATH"],
            "secret_files": [],
            "values": {},
        }
        self.env[(verify.PLATFORM_NAMESPACE, "litellm")] = {
            "names": ["PATH"],
            "secret_files": [],
            "values": {},
        }
        self.env[(verify.PLATFORM_NAMESPACE, SLACK_POSTER)] = {
            "names": ["PATH"],
            "secret_files": ["SLACK_SHARED_BOT_TOKEN"],
            "values": {},
        }

    def __call__(
        self, argv: list[str], *, input: str | None = None, timeout: float = 0.0
    ) -> process.CompletedProcess[str]:
        self.calls.append(list(argv))
        self.inputs.append(input)
        code, out, err = self.answer(list(argv))
        return process.CompletedProcess(argv, code, out, err)

    def answer(self, argv: list[str]) -> tuple[int, str, str]:
        if argv[0] == "aws":
            return self.aws(argv)
        if argv[0] == "kubectl":
            return self.kubectl(argv)
        raise AssertionError(f"unexpected command {argv}")

    def aws(self, argv: list[str]) -> tuple[int, str, str]:
        service, operation = argv[1], argv[2]
        if (service, operation) == ("secretsmanager", "get-resource-policy"):
            readers = self.policies[_option(argv, "--secret-id")]
            policy = {
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"AWS": readers},
                        "Action": verify.GET_VALUE_ACTION,
                    },
                    {"Effect": "Deny", "Principal": "*", "Action": verify.GET_VALUE_ACTION},
                ]
            }
            return 0, json.dumps({"ResourcePolicy": json.dumps(policy)}), ""
        if (service, operation) == ("secretsmanager", "list-secrets"):
            tenant = argv[argv.index("--filters") + 2].split("=")[-1]
            tag = [{"Key": "tenant", "Value": tenant}]
            tagged = [
                {
                    "Name": f"agentic-platform/tenant/{tenant}/github-webhook-secret",
                    "ARN": webhook_secret(tenant),
                    "Tags": tag,
                },
                {
                    "Name": "unrelated",
                    "ARN": secret("unrelated"),
                    "Tags": [{"Key": "team", "Value": tenant}],
                },
                {
                    "Name": f"{tenant}/github-token",
                    "ARN": secret(f"{tenant}/github-token"),
                    "Tags": tag,
                },
            ]
            return 0, json.dumps({"SecretList": tagged}), ""
        if (service, operation) == ("kms", "get-key-policy"):
            return 0, json.dumps({"Policy": "{}"}), ""
        if (service, operation) == ("s3api", "get-bucket-policy"):
            return 0, json.dumps({"Policy": json.dumps(self.bucket_policy)}), ""
        if (service, operation) == ("s3api", "head-object"):
            if self.protocol_object:
                return 0, json.dumps({"ContentLength": 20}), ""
            return (
                254,
                "",
                "An error occurred (404) when calling the HeadObject operation: Not Found",
            )
        if (service, operation) == ("iam", "get-role"):
            trust = {"Statement": [{"Effect": "Allow", "Action": self.trust_actions}]}
            return 0, json.dumps({"Role": {"AssumeRolePolicyDocument": trust}}), ""
        if (service, operation) == ("s3api", "get-bucket-encryption"):
            rule = {"ApplyServerSideEncryptionByDefault": {"KMSMasterKeyID": CONFIG_KEY}}
            return 0, json.dumps({"ServerSideEncryptionConfiguration": {"Rules": [rule]}}), ""
        if (service, operation) == ("ecs", "describe-task-definition"):
            workload = _option(argv, "--task-definition").removeprefix("td-")
            return (
                0,
                json.dumps({"taskDefinition": {"taskRoleArn": role(TASK_ROLES[workload])}}),
                "",
            )
        if (service, operation) == ("iam", "simulate-principal-policy"):
            return 0, json.dumps({"EvaluationResults": [{"EvalDecision": self.simulate(argv)}]}), ""
        if (service, operation) == ("ecs", "list-tasks"):
            return 0, json.dumps({"taskArns": [f"task-{_option(argv, '--service-name')}"]}), ""
        if (service, operation) == ("ecs", "execute-command"):
            if not self.exec_enabled:
                return (
                    254,
                    "",
                    "An error occurred (InvalidParameterException): "
                    "The execute command was not enabled",
                )
            key = f"{_option(argv, '--container')}|{_option(argv, '--command')}"
            return 0, self.exec_output[key], ""
        raise AssertionError(f"unexpected aws call {argv}")

    def simulate(self, argv: list[str]) -> str:
        principal = _option(argv, "--policy-source-arn")
        assert _option(argv, "--caller-arn") == principal
        context = {
            e["ContextKeyName"]: e["ContextKeyValues"][0]
            for e in json.loads(_option(argv, "--context-entries"))
        }
        assert context["aws:PrincipalArn"] == principal
        assert json.loads(_option(argv, "--resource-policy")) is not None
        resource = _option(argv, "--resource-arns")
        if "s3:prefix" in context:
            resource = f"{resource}|{context['s3:prefix']}"
        if context.get("s3:RequestObjectTag/claims-protocol") == "1":
            resource = f"{resource}|tagged"
        allowed = (_option(argv, "--action-names"), resource, principal) in self.allowed
        return "allowed" if allowed else "explicitDeny"

    def kubectl(self, argv: list[str]) -> tuple[int, str, str]:
        namespace = _option(argv, "-n")
        if "get" in argv:
            kind = argv[argv.index("get") + 1]
            if kind == "secretproviderclasses":
                tenant = namespace.removeprefix("tenant-").removesuffix("-review")
                if namespace.endswith("-review"):
                    name, arns = "review-worker", [signing_key(tenant)]
                else:
                    name, arns = "activity-worker", tenant_output(tenant)["secret_arns"].values()
                item = {
                    "metadata": {"name": name},
                    "spec": {
                        "parameters": {"objects": json.dumps([{"objectName": a} for a in arns])}
                    },
                }
                return 0, json.dumps({"items": [item]}), ""
            if namespace == verify.PLATFORM_NAMESPACE:
                poster = _deployment(SLACK_POSTER, [SLACK_POSTER])
                poster["status"] = {"readyReplicas": self.poster_ready}
                items = [
                    _deployment("mcp-gateway-adp", ["mcp-gateway"]),
                    _deployment("litellm", ["litellm"]),
                    poster,
                ]
            elif namespace.endswith("-review"):
                review = _deployment("review-worker", ["review-worker"])
                tenant = namespace.removeprefix("tenant-").removesuffix("-review")
                review["status"] = {"readyReplicas": self.review_ready[tenant]}
                items = [review]
            else:
                items = [_deployment("activity-worker", ["activity-worker", "opa"])]
            return 0, json.dumps({"items": items}), ""
        container = _option(argv, "-c")
        command = argv[argv.index("-") + 1]
        if container == "opa":
            return 1, "", 'exec: "python": executable file not found in $PATH'
        if command == "env-names":
            return 0, _result(self.env[(namespace, container)]), ""
        if command == "poster-tls":
            return 0, _result(self.poster_tls), ""
        if command == "action-key-fingerprints":
            return 0, _result(self.poster_fingerprints), ""
        tenant = namespace.removeprefix("tenant-")
        if command == "review":
            return 0, _result(self.reviews[tenant.removesuffix("-review")]), ""
        if command == "action-key-fingerprint":
            return 0, _result({"fingerprint": self.worker_fingerprints[tenant]}), ""
        if command == "registry-auth":
            return 0, _result({"hosts": {"artifactory.example.gov": 200}}), ""
        if command == "discover":
            names = [REPOSITORIES[tenant]]
            return (
                0,
                _result(
                    {
                        "tenant": tenant,
                        "indexed": names,
                        "indexed_github": names,
                        "registered": names,
                    }
                ),
                "",
            )
        return 0, _result(self.statuses[tenant]), ""


def _deployment(name: str, containers: list[str]) -> dict[str, Any]:
    spec = {"template": {"spec": {"containers": [{"name": c} for c in containers]}}}
    return {"metadata": {"name": name}, "spec": spec}


def run_all(world: World, groups: tuple[str, ...] = verify.GROUPS) -> list[verify.Check]:
    return verify.verify(
        deployment(),
        aws=verify.Aws(world, REGION),
        kube=verify.Kube(world, "sandbox"),
        admin_role_arn=role("platform-admin"),
        repositories={},
        groups=groups,
    )


def failures(checks: list[verify.Check]) -> list[verify.Check]:
    return [check for check in checks if check.status == verify.FAIL]


def find(checks: list[verify.Check], subject: str, name: str) -> verify.Check:
    return next(c for c in checks if c.subject == subject and c.name == name)


def test_load_deployment_reads_terraform_outputs_and_every_nested_secret():
    loaded = deployment()

    assert loaded.region == REGION
    assert (loaded.partition, loaded.account_id) == ("aws", ACCOUNT)
    assert loaded.config_bucket == BUCKET
    adp = loaded.tenants["adp"]
    assert set(adp.secrets) == {
        "github_token",
        "repo_intelligence_token",
        "review_signing_key_arn",
        "mcp_gateway.secret_arns",
    }
    assert adp.worker_secrets == set(tenant_output("adp")["secret_arns"].values())
    assert role("agentic-platform-adp-publisher") in adp.roles
    assert adp.principals()[0] == verify.Principal("adp worker", worker("adp"))
    assert (adp.review_role_arn, adp.review_namespace) == (reviewer("adp"), "tenant-adp-review")
    assert "review-worker" not in loaded.ecs_services
    assert loaded.task_definitions["repo-index-worker"] == "td-repo-index-worker"


def test_load_deployment_accepts_plain_values_and_refuses_no_tenants():
    plain = {name: entry["value"] for name, entry in outputs().items()}

    assert verify.load_deployment(verify.output_values(plain)).tenants.keys() == {"adp", "beta"}
    with pytest.raises(verify.VerifyError, match="no tenants"):
        verify.load_deployment({"tenants": None})


def test_policy_readers_reads_allow_statements_for_the_action():
    policy = json.dumps(
        {
            "Statement": {
                "Effect": "Allow",
                "Principal": {"AWS": role("reader")},
                "Action": "secretsmanager:Get*",
            }
        }
    )

    assert verify.policy_readers(policy, verify.GET_VALUE_ACTION) == [role("reader")]
    assert verify.policy_readers(policy, "secretsmanager:PutSecretValue") == []


def test_a_held_boundary_passes_every_group():
    world = World()

    checks = run_all(world)

    assert failures(checks) == []
    assert {check.group for check in checks} == set(verify.GROUPS)
    skipped = [c for c in checks if c.status == verify.SKIP]
    assert {c.subject.rsplit("/", 1)[-1] for c in skipped} == {"opa"}


def _foreign_delete(world: World) -> None:
    world.allowed.add(("s3:DeleteObject", marker("beta"), publisher("adp")))


def _untagged_upload(world: World) -> None:
    upload = f"{BUCKET_ARN}/tenants/adp/escalation.yaml"
    world.allowed.add(("s3:PutObject", upload, publisher("adp")))


def _probe_lists_every_claim(world: World) -> None:
    world.allowed.add((verify.BUCKET_LIST, f"{BUCKET_ARN}|claims/", claims_probe("adp")))


def _probe_trusts_access_keys(world: World) -> None:
    world.trust_actions = ["sts:AssumeRole"]


def _no_protocol_object(world: World) -> None:
    world.protocol_object = False


def _unreadable_protocol_object(world: World) -> None:
    protocol = f"{BUCKET_ARN}/{CLAIMS_PROTOCOL_KEY}"
    world.allowed.discard(("s3:GetObject", protocol, role(POD_ROLES["slack_poster"])))


def _expiring_leaf(world: World) -> None:
    world.poster_tls = {"ready": 200, "days_left": 20}


def _unverified_leaf(world: World) -> None:
    world.poster_tls = {
        "error": "certificate verification failed (unable to get local issuer certificate)"
    }


def _poster_not_ready(world: World) -> None:
    world.poster_ready = 0


def _tagging_denied(world: World) -> None:
    upload = f"{BUCKET_ARN}/tenants/adp/escalation.yaml"
    world.allowed.discard(("s3:PutObjectTagging", f"{upload}|tagged", publisher("adp")))


def _fingerprint_from_another_key(world: World) -> None:
    world.poster_fingerprints["adp"] = "an-earlier-random-value"


def _fingerprint_not_mounted(world: World) -> None:
    del world.poster_fingerprints["beta"]


@pytest.mark.parametrize(
    ("breach", "subjects"),
    [
        (_foreign_delete, {"adp/claims"}),
        (_untagged_upload, {"adp/claims untagged"}),
        (_probe_lists_every_claim, {"adp/claims probe every claim"}),
        (_probe_trusts_access_keys, {"adp/claims probe", "beta/claims probe"}),
        (_no_protocol_object, {CLAIMS_PROTOCOL_KEY}),
        (_unreadable_protocol_object, {CLAIMS_PROTOCOL_KEY}),
        (_expiring_leaf, {"tenant-adp/activity-worker", "tenant-beta/activity-worker"}),
        (_unverified_leaf, {"tenant-adp/activity-worker", "tenant-beta/activity-worker"}),
        (_poster_not_ready, {SLACK_POSTER}),
        (_tagging_denied, {"adp/claims tagging"}),
        (_fingerprint_from_another_key, {"tenant-adp/activity-worker"}),
        (_fingerprint_not_mounted, {"tenant-beta/activity-worker"}),
    ],
)
def test_each_broken_claims_or_posting_guarantee_fails_its_check(breach, subjects):
    world = World()
    breach(world)

    checks = run_all(world, ("claims", "slack"))

    assert {check.subject for check in failures(checks)} == subjects


def test_without_enforcement_the_upload_checks_are_skipped():
    world = World()
    world.bucket_policy = {"Statement": []}

    checks = run_all(world, ("claims",))

    assert failures(checks) == []
    assert find(checks, "adp/claims", "untagged upload").status == verify.SKIP


def test_the_poster_tls_report_names_a_missing_setting():
    assert (
        "SLACK_POSTING_CA_FILE"
        in verify.poster_tls_report("https://slack-poster:8160", "")["error"]
    )


def test_iam_simulates_each_secret_with_its_resource_policy():
    checks = run_all(World(), ("iam",))

    webhook = "adp/github-webhook-secret"
    assert (
        find(checks, webhook, f"{verify.GET_VALUE_ACTION} as inbound-webhook").status == verify.PASS
    )
    assert "adp/unrelated" not in {check.subject for check in checks}
    beta_denied = find(checks, webhook, f"{verify.GET_VALUE_ACTION} as beta worker")
    assert (beta_denied.expected, beta_denied.observed) == (verify.DENIED, "explicitDeny")
    assert (
        find(checks, webhook, f"{verify.GET_VALUE_ACTION} as platform-admin").expected
        == verify.DENIED
    )
    token = "adp/repo_intelligence_token"
    assert (
        find(checks, token, f"{verify.GET_VALUE_ACTION} as {RI_EXECUTION}").expected
        == verify.ALLOWED
    )
    gateway = "beta/mcp_gateway.secret_arns"
    assert find(checks, gateway, f"{verify.GET_VALUE_ACTION} as beta-gateway").status == verify.PASS
    assert (
        find(checks, gateway, f"{verify.GET_VALUE_ACTION} as adp worker").expected == verify.DENIED
    )
    review = find(checks, "beta/bundle", f"{verify.BUCKET_LIST} as beta review worker")
    assert review.status == verify.PASS
    denied = find(checks, "beta/bundle", f"{verify.OBJECT_READ} as adp review worker")
    assert denied.expected == verify.DENIED
    key = "adp/review_signing_key_arn"
    reader = verify.role_name(reviewer("adp"))
    assert find(checks, key, f"{verify.GET_VALUE_ACTION} as {reader}").status == verify.PASS
    assert find(checks, key, f"{verify.GET_VALUE_ACTION} as beta worker").expected == verify.DENIED


def test_iam_fails_when_another_tenant_reads_a_secret():
    world = World()
    token = tenant_output("adp")["repo_intelligence_token_arn"]
    world.allowed.add((verify.GET_VALUE_ACTION, token, worker("beta")))

    checks = failures(run_all(world, ("iam",)))

    assert [(c.subject, c.name, c.observed) for c in checks] == [
        ("adp/repo_intelligence_token", f"{verify.GET_VALUE_ACTION} as beta worker", "allowed")
    ]


def test_iam_fails_when_the_review_worker_cannot_read_a_bundle():
    world = World()
    world.allowed.discard(
        (
            verify.OBJECT_READ,
            f"arn:aws:s3:::{BUCKET}/tenants/beta/repositories.yaml",
            reviewer("beta"),
        )
    )

    checks = failures(run_all(world, ("iam",)))

    assert [(c.subject, c.name) for c in checks] == [
        ("beta/bundle", f"{verify.OBJECT_READ} as beta review worker")
    ]


def test_iam_fails_when_a_review_worker_reads_another_tenants_bundle():
    world = World()
    bundle = f"arn:aws:s3:::{BUCKET}/tenants/adp/repositories.yaml"
    world.allowed.add((verify.OBJECT_READ, bundle, reviewer("beta")))

    checks = failures(run_all(world, ("iam",)))

    assert [(c.subject, c.name, c.observed) for c in checks] == [
        ("adp/bundle", f"{verify.OBJECT_READ} as beta review worker", "allowed")
    ]


def _review_key_of_another_tenant(world: World) -> None:
    world.reviews["beta"]["signing"] = {
        "fingerprint": "SHA256:adp-key",
        "key_id": "",
        "owners": ["adp"],
    }


def _review_key_outside_the_trust_root(world: World) -> None:
    world.reviews["beta"]["signing"] = {"fingerprint": "SHA256:stray", "key_id": "", "owners": []}


def _review_key_unset(world: World) -> None:
    world.reviews["beta"]["signing"] = {"error": "REVIEW_SIGNING_KEY is not set"}


def _review_worker_not_ready(world: World) -> None:
    world.review_ready["beta"] = 0


@pytest.mark.parametrize(
    ("breach", "observed"),
    [
        (_review_key_of_another_tenant, "a key of adp"),
        (_review_key_outside_the_trust_root, "a key the trust root does not hold"),
        (_review_key_unset, "REVIEW_SIGNING_KEY is not set"),
        (_review_worker_not_ready, "0"),
    ],
)
def test_a_review_worker_without_an_active_key_of_its_tenant_fails(breach, observed):
    world = World()
    breach(world)

    checks = failures(run_all(world, ("review",)))

    assert [(c.subject, c.observed) for c in checks] == [
        ("tenant-beta-review/review-worker", observed)
    ]


def test_the_review_check_names_the_key_by_its_fingerprint_and_key_id():
    checks = run_all(World(), ("review",))

    names = {c.name for c in checks if c.subject == "tenant-adp-review/review-worker"}
    assert "review signing key SHA256:adp-key (adp-review-2026)" in names
    assert "ready replicas" in names


def test_the_review_worker_mounts_and_bundles_are_checked_from_its_pod():
    world = World()
    world.reviews["adp"]["paths"]["/tenants/beta"] = True
    world.reviews["adp"]["bundles"]["beta"] = "read 3 files"

    checks = failures(run_all(world, ("mounts",)))

    assert [(c.subject, c.name, c.observed) for c in checks] == [
        ("tenant-adp-review/review-worker", "/tenants/beta", verify.PRESENT),
        ("tenant-adp-review/review-worker", "bundle beta", "read"),
    ]
    probe = next(c for c in world.calls if "exec" in c and "tenant-adp-review" in c)
    assert probe[probe.index("-") + 1 :] == [
        "review",
        "--repo",
        "agentic-demo-service",
        "--other",
        "beta",
    ]


def test_the_signing_key_report_names_the_active_key_of_the_tenant_only():
    from services.review.trust_root import parse_trust_root

    public = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGq0k2JmBcN9bSx5ZrW0yq6m0m6h8bKk3a1Gm5c8YfQa adp"
    trust_root = parse_trust_root(
        {
            "version": 1,
            "keys": [
                {"id": "adp-review-2025", "tenant": "adp", "public_key": public, "revoked": True},
                {"id": "adp-review-2026", "tenant": "adp", "public_key": public},
            ],
        }
    )

    own = verify.signing_key_report("adp", public, trust_root)
    other = verify.signing_key_report("beta", public, trust_root)

    assert own["key_id"] == "adp-review-2026"
    assert own["fingerprint"].startswith("SHA256:")
    assert "AAAA" not in json.dumps(own)
    assert (other["key_id"], other["owners"]) == ("", ["adp"])


def test_iam_fails_when_a_secret_policy_names_another_tenant():
    world = World()
    world.policies[webhook_secret("adp")].append(worker("beta"))

    checks = failures(run_all(world, ("iam",)))

    assert [(c.subject, c.name) for c in checks] == [
        ("adp/github-webhook-secret", "policy names beta")
    ]


def test_env_fails_on_another_tenant_name_and_the_token_key():
    world = World()
    world.env[("tenant-adp", "activity-worker")]["names"] += [
        "BETA_GITHUB_TOKEN",
        verify.CREDENTIAL_KEY_VARIABLE,
    ]

    checks = failures(run_all(world, ("env",)))

    assert [(c.name, c.observed) for c in checks] == [
        (verify.CREDENTIAL_KEY_VARIABLE, verify.PRESENT),
        ("names of other tenants", "BETA_GITHUB_TOKEN"),
    ]


def test_env_attributes_platform_deployments_to_their_tenant():
    world = World()
    world.env[(verify.PLATFORM_NAMESPACE, "mcp-gateway")]["names"].append("BETA_MCP_TOKEN")
    world.env[(verify.PLATFORM_NAMESPACE, "litellm")]["names"].append("BETA_GITHUB_TOKEN")

    checks = failures(run_all(world, ("env",)))

    assert [(c.subject, c.observed) for c in checks] == [
        (f"{verify.PLATFORM_NAMESPACE}/mcp-gateway-adp/mcp-gateway", "BETA_MCP_TOKEN")
    ]


def test_env_fails_when_a_secret_provider_names_another_tenant_secret():
    providers = [
        {
            "metadata": {"name": "activity-worker"},
            "spec": {
                "parameters": {"objects": json.dumps([{"objectName": secret("beta/webhook")}])}
            },
        }
    ]

    checks = verify.provider_checks("tenant-adp", providers, {secret("beta/webhook"): "beta"})

    assert [(c.observed, c.status) for c in checks] == [("beta", verify.FAIL)]


def test_ri_and_mount_results_map_to_checks():
    world = World()
    world.statuses["adp"]["statuses"]["other"]["beta"][1] = "200"
    world.statuses["adp"]["bundles"]["beta"] = "read 3 files"
    world.statuses["adp"]["paths"]["/tenants/beta"] = True

    checks = failures(run_all(world, ("ri", "mounts")))

    assert [(c.group, c.name, c.observed) for c in checks] == [
        ("ri", "own token, beta/beta-service", "200"),
        ("mounts", "/tenants/beta", verify.PRESENT),
        ("mounts", "bundle beta", "read"),
    ]


def test_probe_runs_the_module_source_behind_the_entrypoint():
    world = World()

    run_all(world, ("ri",))

    probe = next(argv for argv in world.calls if "probe" in argv)
    index = world.calls.index(probe)
    assert probe[:3] == ["kubectl", "--context", "sandbox"]
    assert probe[probe.index("--") + 1 : probe.index("-") + 1] == [verify.ENTRYPOINT, "python", "-"]
    assert world.inputs[index] == Path(verify.__file__).read_text(encoding="utf-8")
    assert "--other-repo" in probe


def test_a_repository_both_tenants_register_is_not_probed_for_403():
    own = verify.Probed("shared", frozenset({"shared", "adp-only"}))

    assert (
        verify.foreign_repository(own, verify.Probed("shared", frozenset({"shared", "beta-only"})))
        == "beta-only"
    )
    assert verify.foreign_repository(own, verify.Probed("shared", frozenset({"shared"}))) == ""


def test_mounts_skip_ecs_when_exec_is_off_and_fail_without_an_index():
    world = World()
    world.exec_enabled = False

    checks = [c for c in run_all(world, ("mounts",)) if c.subject.startswith("ecs/")]

    assert {c.status for c in checks} == {verify.SKIP}

    world = World()
    world.exec_output[f"repo-index-worker|ls -1 {verify.INDEX_ROOTS[0]}/beta"] = "other@abc\n"

    checks = failures(run_all(world, ("mounts",)))

    assert [(c.subject, c.name) for c in checks] == [
        ("ecs/repo-index-worker", "index of beta-service under beta/")
    ]


def test_listing_present_reads_output_not_exit_status():
    path = "/tenants/adp/repositories/r/.git"

    assert verify.listing_present(f"\r\n{path}\r\n", path) == verify.PRESENT
    assert (
        verify.listing_present(f"ls: cannot access '{path}': No such file or directory", path)
        == verify.ABSENT
    )


def test_environment_report_never_returns_values(tmp_path):
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "GITHUB_TOKEN").write_text("ghp-value", encoding="utf-8")
    (secrets_dir / "..data").mkdir()
    environ = tmp_path / "environ"
    entries = [
        "REPO_INTELLIGENCE_TOKEN=token-value",
        "TENANT=adp",
        f"{verify.SECRETS_DIR_VARIABLE}={secrets_dir}",
        "GITHUB_TOKEN=ghp-value",
    ]
    environ.write_bytes("\0".join(entries).encode() + b"\0")

    report = verify.environment_report(environ)

    assert report["names"] == [
        "GITHUB_TOKEN",
        verify.SECRETS_DIR_VARIABLE,
        "REPO_INTELLIGENCE_TOKEN",
        "TENANT",
    ]
    assert report["secret_files"] == ["GITHUB_TOKEN"]
    assert report["values"] == {"TENANT": "adp"}
    assert "token-value" not in json.dumps(report)
    assert "ghp-value" not in json.dumps(report)


def test_worker_report_calls_each_probe_without_returning_tokens():
    seen: list[str] = []

    def status(repository: str) -> str:
        seen.append(repository)
        return "200" if repository == "own" else "403"

    report = verify.worker_report(
        "adp",
        "own",
        ["beta"],
        {"beta": "theirs"},
        status=status,
        anonymous=lambda repository: "401",
        bundle=lambda tenant: "read 2 files" if tenant == "adp" else verify.DENIED,
        exists=lambda path: path.startswith("/tenants/adp"),
    )

    assert seen == ["own", "theirs"]
    assert report["statuses"] == {
        "no_token": "401",
        "own": "200",
        "other": {"beta": ["theirs", "403"]},
    }
    assert report["paths"] == {
        "/tenants/adp": True,
        "/tenants/adp/repositories/own/.git": True,
        "/tenants/beta": False,
    }
    assert report["bundles"] == {"adp": "read 2 files", "beta": verify.DENIED}


def _main(world: World, tmp_path: Path, *args: str) -> int:
    path = tmp_path / "outputs.json"
    path.write_text(json.dumps(outputs()), encoding="utf-8")
    return verify.main(["--outputs", str(path), "--context", "sandbox", *args], run=world)


def test_main_prints_the_table_and_exits_by_result(tmp_path, capsys):
    assert _main(World(), tmp_path, "--only", "iam") == 0
    table = capsys.readouterr().out
    assert table.splitlines()[0].split() == [
        "GROUP",
        "SUBJECT",
        "CHECK",
        "EXPECTED",
        "OBSERVED",
        "RESULT",
    ]
    assert " failed" in table.splitlines()[-1]

    world = World()
    world.allowed.add((verify.GET_VALUE_ACTION, secret("adp/github-token"), role("platform-admin")))
    assert _main(world, tmp_path, "--only", "iam") == 1


def test_main_refuses_unknown_groups_and_bad_repository_pairs(tmp_path, capsys):
    assert _main(World(), tmp_path, "--only", "iam,dns") == 2
    assert _main(World(), tmp_path, "--repo", "adp") == 2
    assert "unknown groups dns" in capsys.readouterr().err


def test_main_reads_terraform_outputs_without_a_file(monkeypatch):
    world = World()
    answer = world.answer

    def with_terraform(argv: list[str]) -> tuple[int, str, str]:
        if argv[0] == "terraform":
            return 0, json.dumps(outputs()), ""
        return answer(argv)

    monkeypatch.setattr(world, "answer", with_terraform)

    assert verify.main(["--only", "iam"], run=world) == 0
    assert world.calls[0][2:] == ["output", "-json"]


def test_main_dispatches_pod_commands(monkeypatch, capsys):
    report: Callable[[list[str]], dict[str, Any]] = lambda args: {"names": ["TENANT"]}  # noqa: E731
    monkeypatch.setitem(verify.POD_COMMANDS, "env-names", report)

    assert verify.main(["env-names"]) == 0
    assert verify.parse_result(capsys.readouterr().out) == {"names": ["TENANT"]}


RUNS: list[tuple[str, dict[str, str]]] = []


class _RecordingContext(Context):
    def run(self, command: str, **kwargs: Any) -> None:  # pyright: ignore[reportIncompatibleMethodOverride]
        RUNS.append((command, dict(kwargs.get("env") or {})))


def test_tenant_verify_aws_task_passes_its_options(monkeypatch):
    monkeypatch.setenv("AWS_REGION", REGION)
    RUNS.clear()
    from scripts.config import get_settings

    get_settings.cache_clear()
    tasks_module.tenant_verify_aws(
        _RecordingContext(), context="sandbox", repo=["adp=agentic-demo-service"], only="iam,ri"
    )
    get_settings.cache_clear()

    [(command, env)] = RUNS
    assert command.endswith(
        "-m scripts.tenant_verify_aws --context sandbox --only iam,ri"
        " --repo adp=agentic-demo-service"
    )
    assert env["AWS_REGION"] == REGION


def test_the_poster_tls_report_reads_ready_and_the_days_left_over_the_trusted_ca(tmp_path):
    import threading
    from datetime import timedelta
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from cryptography.hazmat.primitives import serialization
    from scripts.slack_poster_tls import issue_leaf, new_ca
    from services import server_tls

    class Ready(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200 if self.path == "/ready" else 404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args: object) -> None:
            return

    ca_key, ca = new_ca()
    certificate, key = issue_leaf(ca_key, ca, ["127.0.0.1"], validity=timedelta(days=20))
    (tmp_path / "leaf.pem").write_text(certificate)
    (tmp_path / "key.pem").write_text(key)
    (tmp_path / "ca.pem").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "other.pem").write_bytes(new_ca()[1].public_bytes(serialization.Encoding.PEM))
    server = ThreadingHTTPServer(("127.0.0.1", 0), Ready)
    server_tls.wrap(
        server,
        enabled=True,
        certificate_file=str(tmp_path / "leaf.pem"),
        key_file=str(tmp_path / "key.pem"),
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"https://127.0.0.1:{server.server_address[1]}"
    try:
        trusted = verify.poster_tls_report(url, str(tmp_path / "ca.pem"))
        untrusted = verify.poster_tls_report(url, str(tmp_path / "other.pem"))
    finally:
        server.shutdown()
        server.server_close()

    assert trusted == {"ready": 200, "days_left": 19}
    assert "certificate verification failed" in untrusted["error"]


def test_the_worker_probe_reports_its_action_key_fingerprint_and_never_the_key(monkeypatch):
    from services.integration.tenant_secrets import action_key_fingerprints

    monkeypatch.setenv("ESCALATION_ACTION_KEY", "adp-action-key")

    report = verify.POD_COMMANDS["action-key-fingerprint"]([])

    expected = action_key_fingerprints(["adp"], {"ESCALATION_ACTION_KEY_ADP": "adp-action-key"})
    assert report == {"fingerprint": expected["adp"]}
    assert "adp-action-key" not in json.dumps(report)


def test_the_posting_service_probe_reports_the_fingerprints_it_holds(monkeypatch):
    monkeypatch.setenv("ESCALATION_ACTION_KEY_SHA256_ADP", "ABC123\n")
    monkeypatch.setenv("ESCALATION_ACTION_KEY_SHA256_BETA", "def456")

    report = verify.POD_COMMANDS["action-key-fingerprints"]([])

    assert report == {"adp": "abc123", "beta": "def456"}


REGISTRY_PLATFORM = """config_version: 1
mode: external
mirrors:
  docker.io: artifactory.example.gov/docker-remote
"""
REGISTRY_TENANT = """config_version: 1
mirrors:
  quay.io: artifactory.example.gov/agency-quay
packages:
  pypi: https://artifactory.example.gov/api/pypi/agency/simple
credential:
  username: adp-reader
"""
REGISTRY_PLATFORM_PACKAGES = REGISTRY_PLATFORM + (
    "packages:\n  pypi: https://artifactory.example.gov/api/pypi/pypi-remote/simple\n"
)


def _registry_probe(monkeypatch, tmp_path, respond):
    import httpx
    from tests.support import configure_tenant

    bundle = configure_tenant(monkeypatch, tmp_path)
    (tmp_path / "registries.yaml").write_text(REGISTRY_PLATFORM_PACKAGES)
    (bundle / "registries.yaml").write_text(REGISTRY_TENANT)
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((str(request.url), request.headers.get("authorization", "")))
        return respond(request)

    from httpx._client import Client as client

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client(transport=transport, **kwargs))
    return verify.POD_COMMANDS["registry-auth"](["adp"]), calls


def test_the_registry_probe_reports_each_mirror_and_index_status_and_never_the_token(
    monkeypatch, tmp_path
):
    import base64

    import httpx

    monkeypatch.setenv("REGISTRY_TOKEN", "adp-registry-token")
    report, calls = _registry_probe(monkeypatch, tmp_path, lambda request: httpx.Response(401))

    index = "https://artifactory.example.gov/api/pypi/agency/simple"
    assert report == {"hosts": {"artifactory.example.gov": 401, f"pypi {index}": 401}}
    basic = "Basic " + base64.b64encode(b"adp-reader:adp-registry-token").decode()
    assert calls == [("https://artifactory.example.gov/v2/", basic), (index, basic)]
    assert "adp-registry-token" not in json.dumps(report)


def test_the_registry_probe_follows_a_bearer_challenge_to_its_realm(monkeypatch, tmp_path):
    import httpx

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/":
            challenge = 'Bearer realm="https://auth.example.gov/token",service="artifactory"'
            return httpx.Response(401, headers={"www-authenticate": challenge})
        return httpx.Response(200)

    monkeypatch.setenv("REGISTRY_TOKEN", "adp-registry-token")
    report, calls = _registry_probe(monkeypatch, tmp_path, respond)

    index = "https://artifactory.example.gov/api/pypi/agency/simple"
    assert report == {"hosts": {"artifactory.example.gov": 200, f"pypi {index}": 200}}
    assert calls[1][0] == "https://auth.example.gov/token?service=artifactory"


def test_the_registry_probe_reports_an_unset_token_and_an_unreachable_host(monkeypatch, tmp_path):
    import httpx

    monkeypatch.delenv("REGISTRY_TOKEN", raising=False)
    unset, _ = _registry_probe(monkeypatch, tmp_path / "unset", lambda request: httpx.Response(200))

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    monkeypatch.setenv("REGISTRY_TOKEN", "adp-registry-token")
    unreachable, _ = _registry_probe(monkeypatch, tmp_path / "down", refuse)

    assert unset == {"error": "REGISTRY_TOKEN is not set"}
    assert set(unreachable["hosts"].values()) == {"unreachable ConnectError"}


def test_the_registry_probe_skips_aws_mode(monkeypatch, tmp_path):
    from tests.support import configure_tenant

    configure_tenant(monkeypatch, tmp_path)
    (tmp_path / "registries.yaml").write_text(
        "config_version: 1\nmode: aws\nmirrors:\n  quay.io: 1.dkr.ecr.x.amazonaws.com/quay\n"
    )

    assert verify.POD_COMMANDS["registry-auth"](["adp"]) == {
        "skipped": "aws mode authenticates with the worker's ECR token"
    }


def test_the_registry_probe_skips_a_platform_without_a_registry(monkeypatch, tmp_path):
    from tests.support import configure_tenant

    configure_tenant(monkeypatch, tmp_path)

    assert verify.POD_COMMANDS["registry-auth"](["adp"]) == {
        "skipped": "the platform has no registries.yaml"
    }


class _RegistryKube:
    def __init__(self, reports: dict[str, dict[str, Any]]) -> None:
        self.reports = reports

    def probe(self, namespace, deployment, container, args, *, entrypoint):
        return self.reports[args[1]]


def test_a_registry_report_error_fails_the_tenant_credential():
    kube = _RegistryKube(
        {"adp": {"hosts": {"artifactory.example.gov": 200}}, "beta": {"error": "unset"}}
    )

    checks = verify.registry_checks(deployment(), cast(Any, kube))

    failures = [check for check in checks if check.status == verify.FAIL]
    assert [(check.subject, check.name, check.observed) for check in failures] == [
        ("tenant-beta/activity-worker", "registry credential", "unset")
    ]


def test_a_refused_registry_credential_fails_naming_the_tenant_and_the_host():
    kube = _RegistryKube(
        {
            "adp": {"hosts": {"artifactory.example.gov": 200}},
            "beta": {"hosts": {"artifactory.example.gov": 401}},
        }
    )

    checks = verify.registry_checks(deployment(), cast(Any, kube))

    failures = [check for check in checks if check.status == verify.FAIL]
    assert [(check.subject, check.name, check.observed) for check in failures] == [
        ("tenant-beta/activity-worker", "registry auth artifactory.example.gov", "401")
    ]
