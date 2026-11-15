from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import tasks as tasks_module
from invoke.context import Context
from scripts import tenant_verify_aws as verify
from services import process

ACCOUNT = "123456789012"
REGION = "us-east-1"
BUCKET = "agentic-platform-tenant-config"
CONFIG_KEY = f"arn:aws:kms:{REGION}:{ACCOUNT}:key/config"
CLUSTER = "agentic-platform"
TASK_ROLES = {
    "repo-intelligence": "ri-task",
    "repo-index-worker": "index-task",
    "review-worker": "review-task",
}
RI_EXECUTION = "ri-execution"
REPOSITORIES = {"adp": "agentic-demo-service", "beta": "beta-service"}


def role(name: str) -> str:
    return f"arn:aws:iam::{ACCOUNT}:role/{name}"


def secret(name: str) -> str:
    return f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:{name}-AbCdEf"


def webhook_secret(tenant: str) -> str:
    return secret(f"agentic-platform/tenant/{tenant}/github-webhook-secret")


def worker(tenant: str) -> str:
    return role(f"agentic-platform-tenant-{tenant}")


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
        "publisher_role_arn": role(f"agentic-platform-{tenant}-publisher"),
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
        "versioned_services": {
            "review-worker": {
                "slots": {"blue": "svc-review-blue", "green": "svc-review-green"},
                "task_definition": "td-review-worker",
            }
        },
    }
    tenants = {"config_bucket": BUCKET, "tenants": {t: tenant_output(t) for t in REPOSITORIES}}
    return {
        "tenants": {"value": tenants, "type": ["object", {}], "sensitive": False},
        "eks": {"value": {"region": REGION}, "type": ["object", {}], "sensitive": False},
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
            self.policies[record["mcp_gateway"]["secret_arns"][0]] = [role(f"{tenant}-gateway")]
        for arn, readers in self.policies.items():
            self.allowed |= {(verify.GET_VALUE_ACTION, arn, reader) for reader in readers}
        for tenant in REPOSITORIES:
            record = tenant_output(tenant)
            self.allowed.add(("kms:Decrypt", record["kms_key_arn"], worker(tenant)))
            prefix = f"tenants/{tenant}/"
            for principal in [worker(tenant), *services]:
                self.allowed.add(
                    (verify.OBJECT_READ, f"{bucket_arn}/{prefix}repositories.yaml", principal)
                )
            for principal in (worker(tenant), role(TASK_ROLES["review-worker"])):
                self.allowed.add((verify.BUCKET_LIST, f"{bucket_arn}|{prefix}", principal))
            self.allowed.add(("kms:Decrypt", CONFIG_KEY, worker(tenant)))
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
            for workload in ("repo-index-worker", "review-worker"):
                checkout = f"/tenants/{tenant}/repositories/{REPOSITORIES[tenant]}/.git"
                self.exec_output[f"{workload}|ls -1d {checkout}"] = f"\r\n{checkout}\r\n"
            self.exec_output[f"repo-index-worker|ls -1 {verify.INDEX_ROOTS[0]}/{tenant}"] = (
                f"{REPOSITORIES[tenant]}@abc123\n"
            )
            self.exec_output[f"repo-index-worker|ls -1 {verify.INDEX_ROOTS[1]}/{tenant}"] = (
                f"ls: cannot access '{verify.INDEX_ROOTS[1]}/{tenant}': No such file or directory\n"
            )
            self.exec_output[
                "review-worker|python -m scripts.tenant_verify_aws bundle-read"
                f" --tenant {tenant} --bucket {BUCKET}"
            ] = _result({"tenant": tenant, "bundle": "read 3 files"})
        self.allowed.add(("kms:Decrypt", CONFIG_KEY, role(TASK_ROLES["review-worker"])))
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
            return 0, json.dumps({"Policy": "{}"}), ""
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
        allowed = (_option(argv, "--action-names"), resource, principal) in self.allowed
        return "allowed" if allowed else "explicitDeny"

    def kubectl(self, argv: list[str]) -> tuple[int, str, str]:
        namespace = _option(argv, "-n")
        if "get" in argv:
            kind = argv[argv.index("get") + 1]
            if kind == "secretproviderclasses":
                tenant = namespace.removeprefix("tenant-")
                objects = [
                    {"objectName": arn} for arn in tenant_output(tenant)["secret_arns"].values()
                ]
                item = {
                    "metadata": {"name": "activity-worker"},
                    "spec": {"parameters": {"objects": json.dumps(objects)}},
                }
                return 0, json.dumps({"items": [item]}), ""
            if namespace == verify.PLATFORM_NAMESPACE:
                items = [
                    _deployment("mcp-gateway-adp", ["mcp-gateway"]),
                    _deployment("litellm", ["litellm"]),
                ]
            else:
                items = [_deployment("activity-worker", ["activity-worker", "opa"])]
            return 0, json.dumps({"items": items}), ""
        container = _option(argv, "-c")
        command = argv[argv.index("-") + 1]
        if container == "opa":
            return 1, "", 'exec: "python": executable file not found in $PATH'
        if command == "env-names":
            return 0, _result(self.env[(namespace, container)]), ""
        tenant = namespace.removeprefix("tenant-")
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
        "mcp_gateway.secret_arns",
    }
    assert adp.worker_secrets == set(tenant_output("adp")["secret_arns"].values())
    assert role("agentic-platform-adp-publisher") in adp.roles
    assert adp.principals()[0] == verify.Principal("adp worker", worker("adp"))
    assert loaded.ecs_services["review-worker"] == ("svc-review-blue", "svc-review-green")
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
    review = find(checks, "beta/bundle", f"{verify.BUCKET_LIST} as review-worker task role")
    assert review.status == verify.PASS


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
            role("review-task"),
        )
    )

    checks = failures(run_all(world, ("iam",)))

    assert [(c.subject, c.name) for c in checks] == [
        ("beta/bundle", f"{verify.OBJECT_READ} as review-worker task role")
    ]


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


def test_error_code_reads_a_wrapped_client_error():
    class ClientError(Exception):
        response = {"Error": {"Code": "AccessDenied"}}

    try:
        try:
            raise ClientError
        except ClientError as cause:
            raise RuntimeError("unreadable") from cause
    except RuntimeError as exc:
        assert verify._error_code(exc) == "AccessDenied"


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
