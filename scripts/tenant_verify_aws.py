"""Check the tenant boundary of an AWS deployment with read-only calls and print a result table.

`inv tenant-verify-aws` reads the outputs of the private-bedrock composition root, from
`terraform output -json` or from a file that holds that JSON, and runs four groups of checks.

- iam: for every Secrets Manager secret a tenant's record names or that carries the tag
  tenant=<tenant>, the IAM policy simulator, evaluated with the secret's resource policy, allows
  GetSecretValue to each role the policy names as a reader and to the tenant's worker for each
  secret its worker mounts, and denies it to every role of each other tenant and to the platform
  admin role. The tenant key decrypts through
  Secrets Manager for the tenant's worker and for no other tenant's worker. Evaluated with the
  bucket policy, tenants/<tenant>/ of the config bucket is readable by the tenant's worker,
  repository intelligence, the index worker, and the review worker, and by no other tenant's
  worker.
- env: in every container of every Deployment in each tenant namespace, and of each platform
  Deployment whose name ends in -<tenant>, the environment variable names of the main process and
  the mounted secret file names name no other tenant, and REPO_INTELLIGENCE_TOKEN_KEY is absent.
  The activity worker holds REPO_INTELLIGENCE_TOKEN and its own TENANT and REPOSITORY_ROOT. No
  other platform Deployment holds REPO_INTELLIGENCE_TOKEN_KEY, and no tenant SecretProviderClass
  names a secret of another tenant.
- ri: from each tenant's activity worker, repository intelligence answers 401 to a request
  without a token, 403 to the worker's token for a repository of each other tenant, and 200 to
  the worker's token for the tenant's own indexed repository.
- mounts: the activity worker reads its own config bundle, is refused each other tenant's bundle,
  holds /tenants/<tenant> and the checkout of the probed repository, and holds no other tenant's
  root. Through ECS Exec, the index worker and the review worker hold that checkout, the index
  volume holds an index of the repository under <tenant>/, and the review worker reads the
  tenant's bundle.

The checks make no AWS or Kubernetes write call. A 200 answer from repository intelligence can
refresh a stale index, as any context request does. The table holds variable names, file names,
HTTP status codes, and simulator decisions, never a secret value. The probes inside a tenant pod
run this file's source through `python -` behind the platform entrypoint, so they see the
environment the worker sees. The review worker's bundle read runs this module from its image
through ECS Exec, so it needs an image released with this file.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from services import process

RESULT_MARKER = "TENANT_VERIFY_RESULT "
GROUPS = ("iam", "env", "ri", "mounts")
PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
ALLOWED, DENIED = "allowed", "denied"
PRESENT, ABSENT = "present", "absent"

PLATFORM_NAMESPACE = "agentic-dev-platform"
WORKER = "activity-worker"
ENTRYPOINT = "/usr/local/bin/platform-entrypoint"
TENANTS_ROOT = "/tenants"
INDEX_ROOTS = ("/app/repository_indexes/scip/index", "/app/repository_indexes/zoekt/index")
INDEX_WORKER = "repo-index-worker"
REVIEW_WORKER = "review-worker"
BUNDLE_READERS = ("repo-intelligence", INDEX_WORKER, REVIEW_WORKER)
CREDENTIAL_VARIABLE = "REPO_INTELLIGENCE_TOKEN"
CREDENTIAL_KEY_VARIABLE = "REPO_INTELLIGENCE_TOKEN_KEY"
REPORTED_VALUES = ("TENANT", "REPOSITORY_ROOT")
SECRETS_DIR_VARIABLE = "PLATFORM_SECRETS_DIR"
PROBE_FOCUS = "tenant boundary verification"
ADMIN_ROLE = "platform-admin"
TENANT_TAG = "tenant"
GET_VALUE_ACTION = "secretsmanager:GetSecretValue"
OBJECT_READ = "s3:GetObjectVersion"
BUCKET_LIST = "s3:ListBucketVersions"
COMMAND_TIMEOUT_SECONDS = 600.0
MISSING_EXECUTABLE = ("executable file not found", "no such file or directory")
EXEC_DISABLED = ("execute command was not enabled", "targetnotconnected", "not enabled")

SECRET_ARN = re.compile(r"^arn:[^:]+:secretsmanager:[^:]*:\d{12}:secret:.+")
ROLE_ARN = re.compile(r"^arn:([^:]+):iam::(\d{12}):role/.+")

Runner = Callable[..., process.CompletedProcess[str]]


class VerifyError(Exception):
    """The deployment's outputs or a required call do not allow the checks to run."""


class CommandError(Exception):
    """A read-only command exited with an error."""


@dataclass(frozen=True)
class Check:
    group: str
    subject: str
    name: str
    expected: str
    observed: str
    status: str


def compare(group: str, subject: str, name: str, expected: str, observed: str) -> Check:
    status = PASS if observed == expected else FAIL
    return Check(group, subject, name, expected, observed, status)


def skipped(group: str, subject: str, name: str, reason: str) -> Check:
    return Check(group, subject, name, "", reason, SKIP)


def failed(group: str, subject: str, name: str, expected: str, reason: str) -> Check:
    return Check(group, subject, name, expected, reason, FAIL)


@dataclass(frozen=True)
class Principal:
    label: str
    arn: str


@dataclass(frozen=True)
class TenantRecord:
    name: str
    worker_role_arn: str
    namespace: str
    kms_key_arn: str
    secrets: Mapping[str, str]
    worker_secrets: frozenset[str]
    roles: frozenset[str]

    @property
    def worker(self) -> Principal:
        return Principal(f"{self.name} worker", self.worker_role_arn)

    def principals(self) -> list[Principal]:
        """Every role the tenant's record names, its worker first."""
        others = sorted(self.roles - {self.worker_role_arn})
        return [self.worker, *(Principal(f"{self.name} {role_name(arn)}", arn) for arn in others)]


@dataclass(frozen=True)
class Deployment:
    region: str
    partition: str
    account_id: str
    config_bucket: str
    tenants: Mapping[str, TenantRecord]
    ecs_cluster: str
    ecs_services: Mapping[str, tuple[str, ...]]
    task_definitions: Mapping[str, str]

    @property
    def account_root(self) -> str:
        return f"arn:{self.partition}:iam::{self.account_id}:root"

    def others(self, tenant: str) -> list[TenantRecord]:
        return [record for name, record in sorted(self.tenants.items()) if name != tenant]


def role_name(arn: str) -> str:
    return arn.rsplit("/", 1)[-1]


def output_values(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Return each output's value, from `terraform output -json` or from plain values."""
    values: dict[str, Any] = {}
    for name, entry in raw.items():
        wrapped = isinstance(entry, Mapping) and "value" in entry and "type" in entry
        values[name] = entry["value"] if wrapped else entry
    return values


def _arns(value: Any, pattern: re.Pattern[str], path: str = "") -> list[tuple[str, str]]:
    """Return each ARN matching the pattern at any depth, labelled by its dotted key path."""
    if isinstance(value, str):
        return [(path, value)] if pattern.match(value) else []
    if isinstance(value, Mapping):
        return [
            pair
            for key, item in value.items()
            for pair in _arns(item, pattern, f"{path}.{key}" if path else str(key))
        ]
    if isinstance(value, list | tuple):
        return [pair for item in value for pair in _arns(item, pattern, path)]
    return []


def tenant_record(name: str, record: Mapping[str, Any]) -> TenantRecord:
    """Read a tenant's record, collecting every secret and role ARN it names at any depth.

    The worker mounts the secrets of secret_arns, which also name each secret first, so a secret
    the record names twice keeps its secret_arns key as its label. Secrets listed under one key
    take an index suffix after the first.
    """
    worker_secrets = dict(record.get("secret_arns") or {})
    secrets: dict[str, str] = {}
    for label, arn in [*worker_secrets.items(), *_arns(record, SECRET_ARN)]:
        if arn in secrets.values():
            continue
        unique, index = label, 1
        while unique in secrets:
            unique, index = f"{label}[{index}]", index + 1
        secrets[unique] = arn
    worker_role_arn = record.get("worker_role_arn") or ""
    if not worker_role_arn:
        raise VerifyError(f"the tenants output names no worker_role_arn for {name}")
    roles = frozenset(arn for _, arn in _arns(record, ROLE_ARN)) | {worker_role_arn}
    return TenantRecord(
        name=name,
        worker_role_arn=worker_role_arn,
        namespace=record.get("namespace") or f"tenant-{name}",
        kms_key_arn=record.get("kms_key_arn") or "",
        secrets=secrets,
        worker_secrets=frozenset(worker_secrets.values()),
        roles=roles,
    )


def load_deployment(values: Mapping[str, Any], region: str = "") -> Deployment:
    tenants_output = values.get("tenants")
    if not tenants_output or not tenants_output.get("tenants"):
        raise VerifyError("the tenants output is null. The composition root has no tenants.")
    tenants = {
        name: tenant_record(name, record)
        for name, record in sorted(tenants_output["tenants"].items())
    }
    match = ROLE_ARN.match(next(iter(tenants.values())).worker_role_arn)
    if match is None:
        raise VerifyError("a tenant worker_role_arn is not an IAM role ARN")
    eks = values.get("eks") or {}
    deploy = values.get("container_deploy") or {}
    services: dict[str, tuple[str, ...]] = {
        name: (entry["service"],) for name, entry in (deploy.get("services") or {}).items()
    }
    services.update(
        {
            name: tuple(entry["slots"][slot] for slot in sorted(entry["slots"]))
            for name, entry in (deploy.get("versioned_services") or {}).items()
        }
    )
    task_definitions = {
        name: entry["task_definition"]
        for kind in ("services", "versioned_services")
        for name, entry in (deploy.get(kind) or {}).items()
    }
    return Deployment(
        region=region or eks.get("region") or "",
        partition=match.group(1),
        account_id=match.group(2),
        config_bucket=tenants_output.get("config_bucket") or "",
        tenants=tenants,
        ecs_cluster=deploy.get("cluster") or "",
        ecs_services=services,
        task_definitions=task_definitions,
    )


def policy_readers(policy: str, action: str) -> list[str]:
    """Return the role ARNs an Allow statement of the policy names for the action."""
    readers: list[str] = []
    document = json.loads(policy)
    statements = document.get("Statement") or []
    for statement in statements if isinstance(statements, list) else [statements]:
        if statement.get("Effect") != "Allow":
            continue
        actions = statement.get("Action") or []
        actions = [actions] if isinstance(actions, str) else actions
        if not any(_action_matches(pattern, action) for pattern in actions):
            continue
        principal = statement.get("Principal") or {}
        names = principal.get("AWS", []) if isinstance(principal, Mapping) else []
        for arn in [names] if isinstance(names, str) else names:
            if ROLE_ARN.match(arn) and arn not in readers:
                readers.append(arn)
    return readers


def _action_matches(pattern: str, action: str) -> bool:
    expression = "^" + re.escape(pattern).replace(r"\*", ".*") + "$"
    return re.match(expression, action, flags=re.IGNORECASE) is not None


def run_command(
    argv: Sequence[str], *, input: str | None = None, timeout: float = COMMAND_TIMEOUT_SECONDS
) -> process.CompletedProcess[str]:
    return process.run(list(argv), timeout=timeout, capture_output=True, text=True, input=input)


def _first_line(text: str) -> str:
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    return lines[-1] if lines else "no output"


class Aws:
    """The read-only AWS CLI calls the checks make."""

    def __init__(self, run: Runner, region: str) -> None:
        self.run = run
        self.region = region

    def _call(self, *args: str) -> str:
        argv = ["aws", *args, "--region", self.region, "--output", "json"]
        result = self.run(argv, timeout=COMMAND_TIMEOUT_SECONDS)
        if result.returncode != 0:
            raise CommandError(_first_line(result.stderr or result.stdout))
        return result.stdout

    def _json(self, *args: str) -> Any:
        return json.loads(self._call(*args) or "null")

    def secret_policy(self, arn: str) -> str:
        found = self._json("secretsmanager", "get-resource-policy", "--secret-id", arn)
        return (found or {}).get("ResourcePolicy") or ""

    def key_policy(self, arn: str) -> str:
        found = self._json("kms", "get-key-policy", "--key-id", arn, "--policy-name", "default")
        return found["Policy"]

    def bucket_policy(self, bucket: str) -> str:
        return self._json("s3api", "get-bucket-policy", "--bucket", bucket)["Policy"]

    def bucket_key(self, bucket: str) -> str:
        found = self._json("s3api", "get-bucket-encryption", "--bucket", bucket)
        rules = found["ServerSideEncryptionConfiguration"]["Rules"]
        return rules[0]["ApplyServerSideEncryptionByDefault"].get("KMSMasterKeyID", "")

    def tagged_secrets(self, tenant: str) -> dict[str, str]:
        """Return the name and ARN of each secret that carries the tag tenant=<tenant>."""
        found = self._json(
            "secretsmanager",
            "list-secrets",
            "--filters",
            f"Key=tag-key,Values={TENANT_TAG}",
            f"Key=tag-value,Values={tenant}",
        )
        tag = {"Key": TENANT_TAG, "Value": tenant}
        return {
            entry["Name"]: entry["ARN"]
            for entry in (found or {}).get("SecretList") or []
            if tag in (entry.get("Tags") or [])
        }

    def task_role(self, task_definition: str) -> str:
        found = self._json("ecs", "describe-task-definition", "--task-definition", task_definition)
        return found["taskDefinition"].get("taskRoleArn") or ""

    def simulate(
        self,
        principal: str,
        action: str,
        resource: str,
        *,
        resource_policy: str,
        resource_owner: str,
        context: Mapping[str, tuple[str, str]],
    ) -> str:
        entries = [
            {"ContextKeyName": key, "ContextKeyValues": [value], "ContextKeyType": kind}
            for key, (value, kind) in context.items()
        ]
        found = self._json(
            "iam",
            "simulate-principal-policy",
            "--policy-source-arn",
            principal,
            "--caller-arn",
            principal,
            "--action-names",
            action,
            "--resource-arns",
            resource,
            "--resource-policy",
            resource_policy,
            "--resource-owner",
            resource_owner,
            "--context-entries",
            json.dumps(entries),
        )
        return found["EvaluationResults"][0]["EvalDecision"]

    def running_task(self, cluster: str, services: Iterable[str]) -> str:
        for service in services:
            found = self._json(
                "ecs",
                "list-tasks",
                "--cluster",
                cluster,
                "--service-name",
                service,
                "--desired-status",
                "RUNNING",
            )
            if found and found.get("taskArns"):
                return found["taskArns"][0]
        return ""

    def ecs_exec(self, cluster: str, task: str, container: str, command: str) -> str:
        """Run a command in a task's container and return its output, stdout then stderr.

        The session manager plugin does not pass the command's exit status through, so callers
        read the output.
        """
        argv = ["aws", "ecs", "execute-command", "--region", self.region, "--cluster", cluster]
        argv += ["--task", task, "--container", container, "--interactive", "--command", command]
        result = self.run(argv, timeout=COMMAND_TIMEOUT_SECONDS)
        output = (result.stdout or "") + (result.stderr or "")
        if result.returncode != 0 and not _missing_path(output):
            raise CommandError(_first_line(output))
        return output


def decision(raw: str) -> str:
    return ALLOWED if raw == "allowed" else DENIED


@dataclass(frozen=True)
class Request:
    action: str
    resource: str
    policy: str
    owner: str
    context: Mapping[str, tuple[str, str]] = field(default_factory=dict)


def simulated(
    aws: Aws, subject: str, request: Request, expectations: Iterable[tuple[Principal, str]]
) -> list[Check]:
    """Simulate the request for each principal, naming the principal as aws:PrincipalArn.

    A negated condition operator such as ArnNotEquals matches when its key is absent, so a
    resource policy deny that names readers by aws:PrincipalArn needs the key to evaluate.
    """
    checks: list[Check] = []
    for principal, expected in expectations:
        name = f"{request.action} as {principal.label}"
        context = {"aws:PrincipalArn": (principal.arn, "string"), **request.context}
        try:
            raw = aws.simulate(
                principal.arn,
                request.action,
                request.resource,
                resource_policy=request.policy,
                resource_owner=request.owner,
                context=context,
            )
        except CommandError as exc:
            checks.append(failed("iam", subject, name, expected, f"simulation failed ({exc})"))
            continue
        status = PASS if decision(raw) == expected else FAIL
        checks.append(Check("iam", subject, name, expected, raw, status))
    return checks


def secret_checks(deployment: Deployment, aws: Aws, admin: Principal) -> list[Check]:
    checks: list[Check] = []
    for tenant in deployment.tenants.values():
        others = deployment.others(tenant.name)
        foreign = {arn: other.name for other in others for arn in other.roles}
        for label, arn in tenant.secrets.items():
            subject = f"{tenant.name}/{label}"
            try:
                policy = aws.secret_policy(arn)
            except CommandError as exc:
                checks.append(failed("iam", subject, "resource policy", PRESENT, str(exc)))
                continue
            if not policy:
                checks.append(compare("iam", subject, "resource policy", PRESENT, ABSENT))
                continue
            named = policy_readers(policy, GET_VALUE_ACTION)
            checks += [
                compare("iam", subject, f"policy names {foreign[r]}", "no", role_name(r))
                for r in named
                if r in foreign
            ]
            readers = [
                Principal(role_name(reader), reader)
                for reader in named
                if reader not in foreign and reader not in (tenant.worker_role_arn, admin.arn)
            ]
            if arn in tenant.worker_secrets or tenant.worker_role_arn in named:
                readers.insert(0, tenant.worker)
            denied = [principal for other in others for principal in other.principals()]
            request = Request(GET_VALUE_ACTION, arn, policy, deployment.account_root)
            checks += simulated(
                aws,
                subject,
                request,
                [
                    *((reader, ALLOWED) for reader in readers),
                    *((principal, DENIED) for principal in [*denied, admin]),
                ],
            )
    return checks


def tenant_key_checks(deployment: Deployment, aws: Aws) -> list[Check]:
    checks: list[Check] = []
    via = {"kms:ViaService": (f"secretsmanager.{deployment.region}.amazonaws.com", "string")}
    for tenant in deployment.tenants.values():
        subject = f"{tenant.name}/kms key"
        if not tenant.kms_key_arn:
            checks.append(skipped("iam", subject, "kms:Decrypt", "the record names no key"))
            continue
        try:
            policy = aws.key_policy(tenant.kms_key_arn)
        except CommandError as exc:
            checks.append(failed("iam", subject, "key policy", PRESENT, str(exc)))
            continue
        request = Request("kms:Decrypt", tenant.kms_key_arn, policy, deployment.account_root, via)
        others = [(other.worker, DENIED) for other in deployment.others(tenant.name)]
        checks += simulated(aws, subject, request, [(tenant.worker, ALLOWED), *others])
    return checks


def bundle_reader_roles(deployment: Deployment, aws: Aws) -> tuple[list[Principal], list[Check]]:
    """Return the task roles of the ECS services that read every tenant's bundle."""
    readers: list[Principal] = []
    checks: list[Check] = []
    for workload in BUNDLE_READERS:
        name = f"{workload} task role"
        task_definition = deployment.task_definitions.get(workload)
        if not task_definition:
            checks.append(skipped("iam", "config bucket", name, "no container_deploy"))
            continue
        try:
            readers.append(Principal(name, aws.task_role(task_definition)))
        except CommandError as exc:
            checks.append(failed("iam", "config bucket", name, PRESENT, str(exc)))
    return readers, checks


def bucket_checks(deployment: Deployment, aws: Aws) -> list[Check]:
    if not deployment.config_bucket:
        return [skipped("iam", "config bucket", "bundle reads", "the outputs name no bucket")]
    try:
        policy = aws.bucket_policy(deployment.config_bucket)
        key = aws.bucket_key(deployment.config_bucket)
    except CommandError as exc:
        return [failed("iam", "config bucket", "bucket policy and key", PRESENT, str(exc))]
    services, checks = bundle_reader_roles(deployment, aws)
    review = [principal for principal in services if principal.label.startswith(REVIEW_WORKER)]
    bucket_arn = f"arn:{deployment.partition}:s3:::{deployment.config_bucket}"
    owner = deployment.account_root
    tls = {"aws:SecureTransport": ("true", "boolean")}
    for tenant in deployment.tenants.values():
        subject = f"{tenant.name}/bundle"
        prefix = f"tenants/{tenant.name}/"
        others = [(other.worker, DENIED) for other in deployment.others(tenant.name)]
        read = Request(OBJECT_READ, f"{bucket_arn}/{prefix}repositories.yaml", policy, owner, tls)
        readers = [tenant.worker, *services]
        checks += simulated(aws, subject, read, [*((r, ALLOWED) for r in readers), *others])
        listing = {**tls, "s3:prefix": (prefix, "string")}
        list_request = Request(BUCKET_LIST, bucket_arn, policy, owner, listing)
        listers = [tenant.worker, *review]
        checks += simulated(aws, subject, list_request, [*((r, ALLOWED) for r in listers), *others])
    if not key:
        return checks
    try:
        key_policy = aws.key_policy(key)
    except CommandError as exc:
        return [*checks, failed("iam", "config bucket", "key policy", PRESENT, str(exc))]
    via_s3 = {"kms:ViaService": (f"s3.{deployment.region}.amazonaws.com", "string")}
    decrypt = Request("kms:Decrypt", key, key_policy, owner, via_s3)
    decrypters = [*(tenant.worker for tenant in deployment.tenants.values()), *review]
    return checks + simulated(
        aws, "config bucket/kms key", decrypt, [(p, ALLOWED) for p in decrypters]
    )


def with_tagged_secrets(deployment: Deployment, aws: Aws) -> tuple[Deployment, list[Check]]:
    """Add each secret tagged with a tenant's name that the outputs do not name.

    The tenant module tags every secret it creates, so this covers a secret the composition root
    does not output, such as a tenant's webhook signing secrets.
    """
    tenants: dict[str, TenantRecord] = {}
    checks: list[Check] = []
    for name, tenant in deployment.tenants.items():
        try:
            tagged = aws.tagged_secrets(name)
        except CommandError as exc:
            checks.append(
                failed("iam", name, f"secrets tagged {TENANT_TAG}={name}", "a list", str(exc))
            )
            tagged = {}
        secrets = dict(tenant.secrets)
        for secret_name, arn in sorted(tagged.items()):
            if arn not in secrets.values():
                secrets[secret_name.rsplit("/", 1)[-1]] = arn
        tenants[name] = replace(tenant, secrets=secrets)
    return replace(deployment, tenants=tenants), checks


def iam_checks(deployment: Deployment, aws: Aws, admin_role_arn: str) -> list[Check]:
    admin = Principal(role_name(admin_role_arn), admin_role_arn)
    return [
        *secret_checks(deployment, aws, admin),
        *tenant_key_checks(deployment, aws),
        *bucket_checks(deployment, aws),
    ]


def probe_source() -> str:
    return Path(__file__).read_text(encoding="utf-8")


def parse_result(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.replace("\r", "").splitlines()):
        if line.startswith(RESULT_MARKER):
            return json.loads(line[len(RESULT_MARKER) :])
    raise CommandError(f"the probe printed no result ({_first_line(stdout)})")


class Kube:
    """The read-only kubectl calls the checks make, and the probes they run in a pod."""

    def __init__(self, run: Runner, context: str = "") -> None:
        self.run = run
        self.context = context

    def _base(self, namespace: str) -> list[str]:
        context = ["--context", self.context] if self.context else []
        return ["kubectl", *context, "-n", namespace]

    def _items(self, namespace: str, kind: str) -> list[dict[str, Any]]:
        result = self.run(
            [*self._base(namespace), "get", kind, "-o", "json"], timeout=COMMAND_TIMEOUT_SECONDS
        )
        if result.returncode != 0:
            raise CommandError(_first_line(result.stderr))
        return json.loads(result.stdout or "{}").get("items") or []

    def deployments(self, namespace: str) -> list[dict[str, Any]]:
        return self._items(namespace, "deployments")

    def secret_provider_classes(self, namespace: str) -> list[dict[str, Any]]:
        return self._items(namespace, "secretproviderclasses")

    def probe(
        self,
        namespace: str,
        deployment: str,
        container: str,
        args: Sequence[str],
        *,
        entrypoint: bool,
    ) -> dict[str, Any]:
        command = [ENTRYPOINT] if entrypoint else []
        argv = [
            *self._base(namespace),
            "exec",
            "-i",
            f"deploy/{deployment}",
            "-c",
            container,
            "--",
            *command,
            "python",
            "-",
            *args,
        ]
        result = self.run(argv, input=probe_source(), timeout=COMMAND_TIMEOUT_SECONDS)
        if result.returncode != 0:
            raise CommandError(_first_line(result.stderr or result.stdout))
        return parse_result(result.stdout)


def _segments(name: str) -> set[str]:
    return set(name.upper().split("_"))


def foreign_names(names: Iterable[str], others: Iterable[str]) -> list[str]:
    """Return the names that carry another tenant's name as an underscore-separated part."""
    marks = {other.upper() for other in others}
    return sorted({name for name in names if _segments(name) & marks})


def environment_checks(
    subject: str,
    report: Mapping[str, Any],
    *,
    tenant: str,
    others: Sequence[str],
    worker: bool,
) -> list[Check]:
    names = list(report.get("names") or [])
    files = list(report.get("secret_files") or [])
    values = report.get("values") or {}
    checks = [
        compare(
            "env",
            subject,
            f"{CREDENTIAL_KEY_VARIABLE}",
            ABSENT,
            _presence(CREDENTIAL_KEY_VARIABLE, names + files),
        ),
    ]
    if tenant:
        foreign = foreign_names(names + files, others)
        checks.append(
            compare("env", subject, "names of other tenants", "none", ", ".join(foreign) or "none")
        )
    if worker:
        checks += [
            compare(
                "env", subject, CREDENTIAL_VARIABLE, PRESENT, _presence(CREDENTIAL_VARIABLE, names)
            ),
            compare("env", subject, "TENANT", tenant, str(values.get("TENANT", ""))),
            compare(
                "env",
                subject,
                "REPOSITORY_ROOT",
                f"{TENANTS_ROOT}/{tenant}/repositories",
                str(values.get("REPOSITORY_ROOT", "")),
            ),
        ]
    return checks


def _presence(name: str, names: Iterable[str]) -> str:
    return PRESENT if name in set(names) else ABSENT


def _probe_failure(group: str, subject: str, name: str, exc: CommandError) -> Check:
    message = str(exc)
    if any(marker in message.lower() for marker in MISSING_EXECUTABLE):
        return skipped(group, subject, name, f"no python in the container ({message})")
    return failed(group, subject, name, "a probe result", message)


def _containers(deployment: Mapping[str, Any]) -> list[str]:
    return [c["name"] for c in deployment["spec"]["template"]["spec"].get("containers", [])]


def _workload_env_checks(
    kube: Kube, namespace: str, deployment: Mapping[str, Any], tenant: str, others: Sequence[str]
) -> list[Check]:
    checks: list[Check] = []
    name = deployment["metadata"]["name"]
    for container in _containers(deployment):
        subject = f"{namespace}/{name}/{container}"
        try:
            report = kube.probe(namespace, name, container, ["env-names"], entrypoint=False)
        except CommandError as exc:
            checks.append(_probe_failure("env", subject, "environment names", exc))
            continue
        worker = bool(tenant) and name == WORKER and container == WORKER
        checks += environment_checks(subject, report, tenant=tenant, others=others, worker=worker)
    return checks


def provider_checks(
    namespace: str, providers: Iterable[Mapping[str, Any]], foreign: Mapping[str, str]
) -> list[Check]:
    checks: list[Check] = []
    for provider in providers:
        subject = f"{namespace}/secretproviderclass/{provider['metadata']['name']}"
        objects = json.loads(provider["spec"]["parameters"].get("objects") or "[]")
        named = sorted({foreign[o["objectName"]] for o in objects if o["objectName"] in foreign})
        checks.append(
            compare("env", subject, "secrets of other tenants", "none", ", ".join(named) or "none")
        )
    return checks


def env_checks(deployment: Deployment, kube: Kube) -> list[Check]:
    checks: list[Check] = []
    names = sorted(deployment.tenants)
    for tenant in deployment.tenants.values():
        others = [name for name in names if name != tenant.name]
        foreign = {
            arn: other.name
            for other in deployment.others(tenant.name)
            for arn in other.secrets.values()
        }
        try:
            workloads = kube.deployments(tenant.namespace)
            providers = kube.secret_provider_classes(tenant.namespace)
        except CommandError as exc:
            checks.append(failed("env", tenant.namespace, "list workloads", PRESENT, str(exc)))
            continue
        if not any(d["metadata"]["name"] == WORKER for d in workloads):
            checks.append(compare("env", tenant.namespace, f"deployment {WORKER}", PRESENT, ABSENT))
        for workload in workloads:
            checks += _workload_env_checks(kube, tenant.namespace, workload, tenant.name, others)
        checks += provider_checks(tenant.namespace, providers, foreign)
    try:
        platform = kube.deployments(PLATFORM_NAMESPACE)
    except CommandError as exc:
        return [*checks, failed("env", PLATFORM_NAMESPACE, "list workloads", PRESENT, str(exc))]
    for workload in platform:
        owner = next(
            (name for name in names if workload["metadata"]["name"].endswith(f"-{name}")), ""
        )
        others = [name for name in names if name != owner] if owner else []
        checks += _workload_env_checks(kube, PLATFORM_NAMESPACE, workload, owner, others)
    return checks


def choose_repository(found: Mapping[str, Any]) -> str:
    """Return the first indexed GitHub repository, else the first indexed one."""
    for key in ("indexed_github", "indexed"):
        if found.get(key):
            return found[key][0]
    return ""


@dataclass(frozen=True)
class Probed:
    """The repository probed for a tenant and every repository name its registry holds."""

    repository: str
    registered: frozenset[str] = frozenset()


def discover_repositories(
    deployment: Deployment, kube: Kube, overrides: Mapping[str, str], group: str = "ri"
) -> tuple[dict[str, Probed], list[Check]]:
    """Return each tenant's probed repository, the override or the worker's first indexed one."""
    probed: dict[str, Probed] = {}
    checks: list[Check] = []
    name = "discover the indexed repository"
    for tenant in deployment.tenants.values():
        subject = f"{tenant.namespace}/{WORKER}"
        try:
            found = kube.probe(tenant.namespace, WORKER, WORKER, ["discover"], entrypoint=True)
        except CommandError as exc:
            found = {"error": str(exc)}
        registered = frozenset(found.get("registered") or [])
        repository = overrides.get(tenant.name) or choose_repository(found)
        if repository:
            probed[tenant.name] = Probed(repository, registered)
        else:
            checks.append(failed(group, subject, name, "a name", found.get("error") or "none"))
    return probed, checks


def foreign_repository(own: Probed, other: Probed) -> str:
    """Return a repository of the other tenant that the own tenant's registry does not hold."""
    candidates = [other.repository, *sorted(other.registered)]
    return next((name for name in candidates if name not in own.registered), "")


def worker_probe_checks(
    tenant: TenantRecord, others: Sequence[str], repository: str, report: Mapping[str, Any]
) -> list[Check]:
    subject = f"{tenant.namespace}/{WORKER}"
    statuses = report.get("statuses") or {}
    checks = [
        compare(
            "ri",
            subject,
            f"no token, {tenant.name}/{repository}",
            "401",
            str(statuses.get("no_token")),
        ),
        compare(
            "ri", subject, f"own token, {tenant.name}/{repository}", "200", str(statuses.get("own"))
        ),
    ]
    for other, (other_repository, status) in sorted((statuses.get("other") or {}).items()):
        checks.append(
            compare("ri", subject, f"own token, {other}/{other_repository}", "403", str(status))
        )
    paths = report.get("paths") or {}
    root = f"{TENANTS_ROOT}/{tenant.name}"
    checkout = f"{root}/repositories/{repository}/.git"
    checks.append(compare("mounts", subject, root, PRESENT, _exists(paths, root)))
    checks.append(compare("mounts", subject, checkout, PRESENT, _exists(paths, checkout)))
    for other in others:
        checks.append(
            compare(
                "mounts",
                subject,
                f"{TENANTS_ROOT}/{other}",
                ABSENT,
                _exists(paths, f"{TENANTS_ROOT}/{other}"),
            )
        )
    bundles = report.get("bundles") or {}
    checks.append(
        compare("mounts", subject, f"bundle {tenant.name}", "read", _verb(bundles.get(tenant.name)))
    )
    for other in others:
        checks.append(
            compare("mounts", subject, f"bundle {other}", DENIED, _verb(bundles.get(other)))
        )
    return checks


def _exists(paths: Mapping[str, Any], path: str) -> str:
    if path not in paths:
        return "not probed"
    return PRESENT if paths[path] else ABSENT


def _verb(outcome: Any) -> str:
    return str(outcome or "not probed").split(" ", 1)[0]


def worker_checks(
    deployment: Deployment, kube: Kube, probed: Mapping[str, Probed], group: str = "ri"
) -> list[Check]:
    checks: list[Check] = []
    for tenant in deployment.tenants.values():
        subject = f"{tenant.namespace}/{WORKER}"
        own = probed.get(tenant.name)
        if own is None:
            checks.append(skipped(group, subject, "worker probe", "no probed repository"))
            continue
        others = [other.name for other in deployment.others(tenant.name)]
        args = ["probe", "--repo", own.repository]
        for other in others:
            args += ["--other", other]
            foreign = foreign_repository(own, probed[other]) if other in probed else ""
            if foreign:
                args += ["--other-repo", f"{other}={foreign}"]
            else:
                checks.append(
                    skipped("ri", subject, f"own token, {other}", "no repository only it holds")
                )
        try:
            report = kube.probe(tenant.namespace, WORKER, WORKER, args, entrypoint=True)
        except CommandError as exc:
            checks.append(failed(group, subject, "worker probe", "a probe result", str(exc)))
            continue
        checks += worker_probe_checks(tenant, others, own.repository, report)
    return checks


def _missing_path(output: str) -> bool:
    lowered = output.lower()
    return "no such file" in lowered or "cannot access" in lowered


def listing_present(output: str, path: str) -> str:
    lines = [line.strip() for line in output.replace("\r", "").splitlines()]
    return PRESENT if path in lines and not _missing_path(output) else ABSENT


def index_present(output: str, repository: str) -> bool:
    lines = [line.strip() for line in output.replace("\r", "").splitlines()]
    return any(line.startswith(f"{repository}@") for line in lines)


def _exec_failure(subject: str, name: str, exc: CommandError) -> Check:
    message = str(exc)
    if any(marker in message.lower() for marker in EXEC_DISABLED):
        return skipped("mounts", subject, name, f"ECS Exec is off ({message})")
    return failed("mounts", subject, name, "a command result", message)


def ecs_checks(deployment: Deployment, aws: Aws, repositories: Mapping[str, str]) -> list[Check]:
    checks: list[Check] = []
    for workload in (INDEX_WORKER, REVIEW_WORKER):
        services = deployment.ecs_services.get(workload)
        if not deployment.ecs_cluster or not services:
            checks.append(skipped("mounts", workload, "ECS task", "no container_deploy service"))
            continue
        try:
            task = aws.running_task(deployment.ecs_cluster, services)
        except CommandError as exc:
            checks.append(failed("mounts", workload, "ECS task", "running", str(exc)))
            continue
        if not task:
            checks.append(compare("mounts", workload, "ECS task", "running", "none"))
            continue
        for tenant in deployment.tenants:
            checks += _ecs_tenant_checks(
                deployment, aws, workload, task, tenant, repositories.get(tenant)
            )
    return checks


def _ecs_tenant_checks(
    deployment: Deployment, aws: Aws, workload: str, task: str, tenant: str, repository: str | None
) -> list[Check]:
    subject = f"ecs/{workload}"

    def run(command: str) -> str:
        return aws.ecs_exec(deployment.ecs_cluster, task, workload, command)

    if not repository:
        return [skipped("mounts", subject, f"{tenant} checkout", "no probed repository")]
    checks: list[Check] = []
    checkout = f"{TENANTS_ROOT}/{tenant}/repositories/{repository}/.git"
    try:
        checks.append(
            compare(
                "mounts",
                subject,
                checkout,
                PRESENT,
                listing_present(run(f"ls -1d {checkout}"), checkout),
            )
        )
    except CommandError as exc:
        return [_exec_failure(subject, checkout, exc)]
    if workload == INDEX_WORKER:
        found = False
        for root in INDEX_ROOTS:
            try:
                found = found or index_present(run(f"ls -1 {root}/{tenant}"), repository)
            except CommandError as exc:
                checks.append(_exec_failure(subject, f"{root}/{tenant}", exc))
        observed = PRESENT if found else ABSENT
        checks.append(
            compare("mounts", subject, f"index of {repository} under {tenant}/", PRESENT, observed)
        )
    if workload == REVIEW_WORKER:
        try:
            report = parse_result(
                run(
                    "python -m scripts.tenant_verify_aws bundle-read"
                    f" --tenant {tenant} --bucket {deployment.config_bucket}"
                )
            )
            observed = _verb(report.get("bundle"))
        except CommandError as exc:
            observed = str(exc)
        checks.append(compare("mounts", subject, f"bundle {tenant}", "read", observed))
    return checks


def verify(
    deployment: Deployment,
    *,
    aws: Aws,
    kube: Kube,
    admin_role_arn: str,
    repositories: Mapping[str, str],
    groups: Iterable[str] = GROUPS,
) -> list[Check]:
    selected = set(groups)
    checks: list[Check] = []
    if selected & {"iam", "env"}:
        deployment, listing = with_tagged_secrets(deployment, aws)
        checks += listing
    if "iam" in selected:
        checks += iam_checks(deployment, aws, admin_role_arn)
    if "env" in selected:
        checks += env_checks(deployment, kube)
    if selected & {"ri", "mounts"}:
        group = "ri" if "ri" in selected else "mounts"
        probed, discovery = discover_repositories(deployment, kube, repositories, group)
        probes = [*discovery, *worker_checks(deployment, kube, probed, group)]
        checks += [check for check in probes if check.group in selected]
        if "mounts" in selected:
            found = {tenant: entry.repository for tenant, entry in probed.items()}
            checks += ecs_checks(deployment, aws, found)
    return checks


def render(checks: Sequence[Check]) -> str:
    header = ("GROUP", "SUBJECT", "CHECK", "EXPECTED", "OBSERVED", "RESULT")
    rows = [header] + [
        (c.group, c.subject, c.name, c.expected, c.observed[:80], c.status) for c in checks
    ]
    widths = [max(len(row[index]) for row in rows) for index in range(len(header))]
    lines = [
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()
        for row in rows
    ]
    counts = {status: sum(1 for c in checks if c.status == status) for status in (PASS, FAIL, SKIP)}
    lines.append("")
    lines.append(f"{counts[PASS]} passed, {counts[FAIL]} failed, {counts[SKIP]} skipped")
    return "\n".join(lines)


def terraform_outputs(run: Runner) -> dict[str, Any]:
    from scripts.temporal_certs import COMPOSITION_ROOT

    result = run(
        ["terraform", f"-chdir={COMPOSITION_ROOT}", "output", "-json"],
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise VerifyError(f"terraform output failed ({_first_line(result.stderr)})")
    return json.loads(result.stdout or "{}")


def parse_pairs(pairs: Iterable[str], option: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for pair in pairs:
        tenant, separator, value = pair.partition("=")
        if not separator or not tenant or not value:
            raise VerifyError(f"{option} takes TENANT=VALUE, not {pair!r}")
        parsed[tenant] = value
    return parsed


def main(argv: list[str] | None = None, *, run: Runner = run_command) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in POD_COMMANDS:
        print(RESULT_MARKER + json.dumps(POD_COMMANDS[args[0]](args[1:])))
        return 0
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n", 1)[0])
    parser.add_argument("--outputs", default="", help="File holding `terraform output -json`.")
    parser.add_argument("--context", default="", help="kubectl context of the EKS cluster.")
    parser.add_argument("--region", default="", help="AWS region (default the eks output's).")
    parser.add_argument(
        "--admin-role-arn", default="", help="Role that must read no tenant secret."
    )
    parser.add_argument("--repo", action="append", default=[], help="TENANT=REPOSITORY to probe.")
    parser.add_argument(
        "--only", default="", help=f"Comma-separated groups of {', '.join(GROUPS)}."
    )
    options = parser.parse_args(args)
    try:
        raw = (
            json.loads(Path(options.outputs).read_text(encoding="utf-8"))
            if options.outputs
            else terraform_outputs(run)
        )
        deployment = load_deployment(output_values(raw), options.region)
        if not deployment.region:
            raise VerifyError("no region. Pass --region or apply the composition root with EKS.")
        groups = [group.strip() for group in options.only.split(",") if group.strip()] or list(
            GROUPS
        )
        unknown = sorted(set(groups) - set(GROUPS))
        if unknown:
            raise VerifyError(f"unknown groups {', '.join(unknown)}")
        admin = options.admin_role_arn or (
            f"arn:{deployment.partition}:iam::{deployment.account_id}:role/{ADMIN_ROLE}"
        )
        checks = verify(
            deployment,
            aws=Aws(run, deployment.region),
            kube=Kube(run, options.context),
            admin_role_arn=admin,
            repositories=parse_pairs(options.repo, "--repo"),
            groups=groups,
        )
    except (VerifyError, FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"[tenant-verify-aws] {exc}", file=sys.stderr)
        return 2
    print(render(checks))
    return 1 if any(check.status == FAIL for check in checks) else 0


def environment_report(environ: Path = Path("/proc/1/environ")) -> dict[str, Any]:
    """Return the variable names of the container's main process and its secret file names.

    Only the values REPORTED_VALUES names leave the pod, and the secrets directory is read for
    file names only.
    """
    names: set[str] = set()
    values: dict[str, str] = {}
    secrets_dir = ""
    for entry in environ.read_bytes().split(b"\0"):
        if not entry:
            continue
        raw_name, _, raw_value = entry.partition(b"=")
        name = raw_name.decode(errors="replace")
        names.add(name)
        if name in REPORTED_VALUES:
            values[name] = raw_value.decode(errors="replace")
        elif name == SECRETS_DIR_VARIABLE:
            secrets_dir = raw_value.decode(errors="replace")
    directory = Path(secrets_dir) if secrets_dir else None
    files = (
        sorted(path.name for path in directory.iterdir() if not path.name.startswith("."))
        if directory is not None and directory.is_dir()
        else []
    )
    return {"names": sorted(names), "secret_files": files, "values": values}


def _env_names(args: list[str]) -> dict[str, Any]:
    return environment_report()


def _tenant_registry(tenant: str) -> Any:
    from services.integration.credentials import env_config_dir
    from services.integration.tenant_config import tenant_source
    from services.repository_intelligence.tenants import parse_tenant_repositories

    from scripts.config import get_settings

    settings = get_settings()
    source = tenant_source(settings, tenant)
    bundle = source.fetch(source.latest())
    text = bundle.files.get("repositories")
    if text is None:
        text = (env_config_dir(settings.environment) / "repositories.yaml").read_text(
            encoding="utf-8"
        )
    return parse_tenant_repositories(text, tenant)


def _discover(args: list[str]) -> dict[str, Any]:
    from services.integration.repositories import GITHUB

    from scripts.config import get_settings

    tenant = get_settings().tenant
    try:
        registry = _tenant_registry(tenant)
    except Exception as exc:  # noqa: BLE001 - the report names any failure to read the bundle
        return {"tenant": tenant, "error": f"{type(exc).__name__} ({exc})"}
    indexed = sorted(name for name, entry in registry.repositories.items() if entry.index)
    github = [name for name in indexed if registry.repositories[name].backend == GITHUB]
    registered = sorted(registry.repositories)
    return {
        "tenant": tenant,
        "indexed": indexed,
        "indexed_github": github,
        "registered": registered,
    }


def bundle_outcome(tenant: str, bucket: str = "") -> str:
    """Read the latest bundle of a tenant through this process's identity, as the services do."""
    from services.integration.tenant_config import S3, tenant_source

    from scripts.config import get_settings

    settings = get_settings()
    if bucket:
        settings = settings.model_copy(update={"tenant_config_bucket": bucket})
    if not settings.tenant_config_bucket:
        return "unset TENANT_CONFIG_BUCKET"
    try:
        source = tenant_source(settings, tenant)
        identity = source.latest()
        bundle = source.fetch(identity)
    except Exception as exc:  # noqa: BLE001 - the outcome names any refusal or failure
        return DENIED if _error_code(exc) == "AccessDenied" else f"error {type(exc).__name__}"
    if identity.source != S3:
        return f"error {identity.source} source"
    if not bundle.files:
        return "empty"
    return f"read {len(bundle.files)} files"


def _error_code(exc: BaseException) -> str:
    for error in (exc, exc.__cause__):
        response = getattr(error, "response", None)
        if isinstance(response, Mapping) and response.get("Error", {}).get("Code"):
            return str(response["Error"]["Code"])
    return ""


def _bundle_read(args: list[str]) -> dict[str, Any]:
    parser = argparse.ArgumentParser(prog="bundle-read")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--bucket", default="")
    options = parser.parse_args(args)
    return {"tenant": options.tenant, "bundle": bundle_outcome(options.tenant, options.bucket)}


def context_status(repository: str) -> str:
    """POST /context_package with this worker's token through the platform client."""
    import asyncio

    import httpx
    from services.orchestration.repo_intelligence_client import fetch_context_package

    try:
        asyncio.run(fetch_context_package(repository, "", PROBE_FOCUS))
    except httpx.HTTPStatusError as exc:
        return str(exc.response.status_code)
    except Exception as exc:  # noqa: BLE001 - the status names any failure to reach the service
        return f"error {type(exc).__name__}"
    return "200"


def anonymous_status(repository: str) -> str:
    """POST /context_package without an Authorization header."""
    import httpx

    from scripts.config import get_settings

    settings = get_settings()
    payload = {"repo": repository, "commit": "", "focus": PROBE_FOCUS}
    try:
        with httpx.Client(base_url=settings.repo_intelligence_url, timeout=60.0) as client:
            return str(client.post("/context_package", json=payload).status_code)
    except Exception as exc:  # noqa: BLE001 - the status names any failure to reach the service
        return f"error {type(exc).__name__}"


def worker_report(
    tenant: str,
    repository: str,
    others: Sequence[str],
    other_repositories: Mapping[str, str],
    *,
    status: Callable[[str], str] = context_status,
    anonymous: Callable[[str], str] = anonymous_status,
    bundle: Callable[[str], str] = bundle_outcome,
    exists: Callable[[str], bool] = lambda path: Path(path).exists(),
) -> dict[str, Any]:
    root = f"{TENANTS_ROOT}/{tenant}"
    paths = [
        root,
        f"{root}/repositories/{repository}/.git",
        *(f"{TENANTS_ROOT}/{o}" for o in others),
    ]
    return {
        "tenant": tenant,
        "statuses": {
            "no_token": anonymous(repository),
            "own": status(repository),
            "other": {
                other: [name, status(name)] for other, name in sorted(other_repositories.items())
            },
        },
        "paths": {path: exists(path) for path in paths},
        "bundles": {name: bundle(name) for name in [tenant, *others]},
    }


def _probe(args: list[str]) -> dict[str, Any]:
    from scripts.config import get_settings

    parser = argparse.ArgumentParser(prog="probe")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--other", action="append", default=[])
    parser.add_argument("--other-repo", action="append", default=[])
    options = parser.parse_args(args)
    return worker_report(
        get_settings().tenant,
        options.repo,
        options.other,
        parse_pairs(options.other_repo, "--other-repo"),
    )


POD_COMMANDS: dict[str, Callable[[list[str]], dict[str, Any]]] = {
    "env-names": _env_names,
    "discover": _discover,
    "probe": _probe,
    "bundle-read": _bundle_read,
}


if __name__ == "__main__":
    raise SystemExit(main())
