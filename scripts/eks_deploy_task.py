"""Deploy the EKS workloads from inside the VPC, restart them, or scale them for the night.

This is the entrypoint of the eks-deployer image, which `inv eks-deploy`, the secret store's
restart rule, and the off-hours automation start as a one-shot ECS task in the management tier.
The task definition carries the environment's values in DEPLOY_INPUTS, and the image carries
kubectl, helm, the aws-iam-authenticator token helper, the vendored AWS Load Balancer Controller
chart, and the AWS overlay, so the workstation needs none of them.

- `deploy` decides the worker slots, installs or upgrades the Load Balancer Controller, renders
  the overlay for the environment, applies it, waits for each deployment, and waits for the load
  balancer of each tenant's MCP gateway Ingress. It lists every pod that is not host-networked
  and that no NetworkPolicy selects, which strict network policy mode would deny, and ends by
  running the deployment validation of every tenant, whose failures it prints without failing the
  deploy.
- `preflight` decides the worker slots for the inputs' build and prints the decision without
  changing anything, and exits 1 when a worker has no slot.
- `restart` rolls the named deployments, `namespace/name` or a platform deployment by name,
  `tenant-workers` for every tenant's activity worker, `tenant-review-workers` for every tenant's
  review worker, and `mcp-gateway` for every tenant's MCP gateway, so their pods read the current
  secret values.
- `check-policies` prints the pods no NetworkPolicy selects and exits 1 when there are any.
- `scale-down` records the replica count of each named deployment in an annotation, scales it to
  zero, and waits until none of its pods runs. The off-hours sleep runs it.
- `scale-up` restores the recorded replica count of each named deployment that has one, removes
  the annotation, and waits for the rollout. The off-hours wake runs it.
- `check-stopped` prints each running pod of the named deployments and exits 1 when there are any.

Each tenant's activity worker and review worker runs in two slots, the current Deployment and a
`<name>-drain` Deployment that keeps the previous build while executions are pinned to it. The
decision reads each slot's build from its worker image and runs `scripts.pinned_executions` as a
Job for each build it must check. A build that differs from the current slot's build moves the
current slot's live spec into the drain slot first when runs are pinned to it, and the deploy
stops before any change when the drain slot holds a build with pinned runs too. The render holds
only the current slot, so the apply never changes a drain slot's image, and a drain slot whose
build has no pinned runs is deleted. Every command that names a tenant worker, directly or
through a group, also acts on its drain slot when one exists.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import tempfile
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from services import process

from scripts.eks_render import (
    PLATFORM_NAMESPACE,
    RenderError,
    render,
    termination_grace,
    workloads_last,
)
from scripts.worker_slots import (
    PinnedRuns,
    SlotRefusal,
    SlotState,
    choose_slots,
    image_build,
    may_stop,
)

KUBECTL = "/usr/local/bin/kubectl"
HELM = "/usr/local/bin/helm"
AUTHENTICATOR = "/usr/local/bin/aws-iam-authenticator"
ROOT = Path(os.environ.get("EKS_DEPLOYER_ROOT", "/opt/eks-deployer"))
OVERLAY = ROOT / "deploy" / "kubernetes" / "aws"
CHART = ROOT / "charts" / "aws-load-balancer-controller.tgz"
DEPLOYMENTS = ("litellm", "inbound-webhook")
COMMAND_TIMEOUT = 300
CLAIM_DELETE_SECONDS = 300
ROLLOUT_SECONDS = 600
ROLLOUT_TIMEOUT = f"{ROLLOUT_SECONDS}s"
SCALE_UP_SECONDS = 300
LOG = "[eks-deployer]"
RUNNING_POD = f"{LOG} running pod "
REPLICAS_ANNOTATION = "off-hours.agentic-dev-platform/replicas"
POD_EXIT_SECONDS = 300
POLL_SECONDS = 5
PINNED_ENTRY = "scripts.pinned_executions"
PINNED_EXIT_CODE = 3
PINNED_LOG_TAIL_LINES = 20
# Annotations that kubectl and the Deployment controller write about the live current slot.
SERVER_ANNOTATIONS = (
    "kubectl.kubernetes.io/last-applied-configuration",
    "deployment.kubernetes.io/revision",
)


class DeployError(Exception):
    """A deploy step failed."""


def run(args: list[str], *, stdin: str | None = None, timeout: int = COMMAND_TIMEOUT) -> str:
    result = process.run(args, input=stdin, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise DeployError(f"{Path(args[0]).name} {args[1]} failed: {result.stderr.strip()}")
    return result.stdout


def write_kubeconfig(cluster: Mapping[str, str], path: Path) -> None:
    """Write a kubeconfig whose token the authenticator mints from the task role on each call."""
    config = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [
            {
                "name": cluster["name"],
                "cluster": {
                    "server": cluster["endpoint"],
                    "certificate-authority-data": cluster["certificate_authority"],
                },
            }
        ],
        "users": [
            {
                "name": "eks-deployer",
                "user": {
                    "exec": {
                        "apiVersion": "client.authentication.k8s.io/v1beta1",
                        "command": AUTHENTICATOR,
                        "args": ["token", "-i", cluster["name"]],
                        "env": [{"name": "AWS_REGION", "value": cluster["region"]}],
                        "interactiveMode": "Never",
                    }
                },
            }
        ],
        "contexts": [
            {"name": "eks", "context": {"cluster": cluster["name"], "user": "eks-deployer"}}
        ],
        "current-context": "eks",
    }
    path.write_text(json.dumps(config), encoding="utf-8")


def overlay_documents() -> list[dict[str, Any]]:
    """Render the overlay and convert it to JSON through kubectl, which needs no YAML parser."""
    rendered = run(
        [KUBECTL, "kustomize", "--load-restrictor", "LoadRestrictionsNone", str(OVERLAY)]
    )
    converted = run(
        [KUBECTL, "create", "--dry-run=client", "-o", "json", "-f", "-"], stdin=rendered
    )
    return json_objects(converted)


def json_objects(text: str) -> list[dict[str, Any]]:
    """Read the JSON objects kubectl prints one after another for a multi-document input."""
    decoder = json.JSONDecoder()
    objects: list[dict[str, Any]] = []
    index = 0
    while True:
        while index < len(text) and text[index].isspace():
            index += 1
        if index == len(text):
            return objects
        value, index = decoder.raw_decode(text, index)
        objects.extend(value["items"] if value.get("kind") == "List" else [value])


def apply(documents: list[dict[str, Any]]) -> None:
    listing = {"apiVersion": "v1", "kind": "List", "items": workloads_last(documents)}
    print(run([KUBECTL, "apply", "-f", "-"], stdin=json.dumps(listing)), end="")


def install_controller(inputs: Mapping[str, Any]) -> None:
    controller = inputs["load_balancer_controller"]
    cluster = inputs["cluster"]
    print(f"{LOG} installing the AWS Load Balancer Controller {controller['app_version']}")
    run(
        [
            HELM,
            "upgrade",
            "--install",
            "aws-load-balancer-controller",
            str(CHART),
            "--namespace",
            controller["namespace"],
            "--set",
            f"clusterName={cluster['name']}",
            "--set",
            f"region={cluster['region']}",
            "--set",
            f"vpcId={cluster['vpc_id']}",
            "--set",
            "serviceAccount.create=true",
            "--set",
            f"serviceAccount.name={controller['service_account']}",
            "--set",
            f"image.repository={controller['image_repository']}",
            "--set",
            f"image.tag={controller['app_version']}",
            "--set",
            "enableServiceMutatorWebhook=false",
            "--set",
            "keepTLSSecret=true",
            "--set-json",
            f"nodeSelector={json.dumps({inputs['node_pool_label']: 'platform'})}",
            "--wait",
            "--timeout",
            ROLLOUT_TIMEOUT,
        ],
        timeout=900,
    )
    # The controller reads its webhook certificate at start, and a first install or a chart
    # change can replace the certificate the API server trusts, so the pods restart onto it.
    name = "deployment/aws-load-balancer-controller"
    namespace = controller["namespace"]
    run([KUBECTL, "-n", namespace, "rollout", "restart", name])
    run(
        [KUBECTL, "-n", namespace, "rollout", "status", name, f"--timeout={ROLLOUT_TIMEOUT}"],
        timeout=900,
    )


def _namespaced(target: str) -> tuple[str, str]:
    """Split `namespace/name`, where a bare name is a deployment of the platform namespace."""
    namespace, _, name = target.rpartition("/")
    return namespace or PLATFORM_NAMESPACE, name


def tenant_workers(inputs: Mapping[str, Any]) -> list[str]:
    from scripts.tenant_render import WORKER, worker_namespace

    return [f"{worker_namespace(entry['name'])}/{WORKER}" for entry in inputs.get("tenants") or []]


def tenant_review_workers(inputs: Mapping[str, Any]) -> list[str]:
    from scripts.tenant_render import REVIEW_WORKER, review_namespace

    return [
        f"{review_namespace(entry['name'])}/{REVIEW_WORKER}"
        for entry in inputs.get("tenants") or []
    ]


def tenant_gateways(inputs: Mapping[str, Any]) -> list[str]:
    from scripts.tenant_render import GATEWAY, worker_namespace

    return [f"{worker_namespace(entry['name'])}/{GATEWAY}" for entry in inputs.get("tenants") or []]


def rollout(
    targets: Iterable[str], seconds: int = ROLLOUT_SECONDS, *, replaces_pods: bool = True
) -> None:
    """Wait for each Deployment to roll out and for the pods it replaces to stop."""
    for target in targets:
        namespace, name = _namespaced(target)
        wait = seconds + (termination_grace(get_deployment(target)) if replaces_pods else 0)
        try:
            run(
                [
                    KUBECTL,
                    "-n",
                    namespace,
                    "rollout",
                    "status",
                    f"deployment/{name}",
                    f"--timeout={wait}s",
                ],
                timeout=wait + 60,
            )
        except DeployError:
            diagnose(name, namespace)
            raise
        print(f"{LOG} {namespace}/deployment/{name} is rolled out")


def replace_changed_claims(documents: list[dict[str, Any]]) -> dict[str, int]:
    """Delete each live claim whose access modes differ from its rendered claim, and its volume.

    A bound claim's spec is immutable, so an apply refuses the change. The claim's namespace
    scales its deployments to zero first, because the protection finalizer keeps a claim a pod
    mounts. The apply that follows recreates the claim and the volume and restores the replicas
    of the rendered deployments. A drain slot is not rendered, so this returns the replica count
    of each drain slot it scaled down, for `restore_replicas` after the apply.
    """
    drains: dict[str, int] = {}
    for claim in (d for d in documents if d.get("kind") == "PersistentVolumeClaim"):
        namespace, name = claim["metadata"]["namespace"], claim["metadata"]["name"]
        live = run(
            [KUBECTL, "-n", namespace, "get", "pvc", name, "-o", "json", "--ignore-not-found"]
        )
        if not live.strip():
            continue
        current = json.loads(live)["spec"]
        wanted = claim["spec"].get("accessModes")
        if current.get("accessModes") == wanted:
            continue
        print(
            f"{LOG} recreating {namespace}/{name}, whose access modes change from "
            f"{current.get('accessModes')} to {wanted}"
        )
        drains.update(_drain_replicas(namespace))
        run([KUBECTL, "-n", namespace, "scale", "deployment", "--all", "--replicas=0"])
        seconds = CLAIM_DELETE_SECONDS + max(
            (
                termination_grace(d)
                for d in documents
                if d.get("kind") == "Deployment" and d["metadata"].get("namespace") == namespace
            ),
            default=0,
        )
        wait = ["--wait=true", f"--timeout={seconds}s"]
        run([KUBECTL, "-n", namespace, "delete", "pvc", name, *wait], timeout=seconds + 60)
        if current.get("volumeName"):
            run(
                [KUBECTL, "delete", "pv", current["volumeName"], "--ignore-not-found", *wait],
                timeout=seconds + 60,
            )
    return drains


def _drain_replicas(namespace: str) -> dict[str, int]:
    from scripts.tenant_render import REVIEW_WORKER, WORKER

    replicas = {}
    for name in (WORKER, REVIEW_WORKER):
        target = drain_of(f"{namespace}/{name}")
        deployment = get_deployment(target)
        if deployment is not None and deployment["spec"].get("replicas"):
            replicas[target] = deployment["spec"]["replicas"]
    return replicas


def restore_replicas(replicas: Mapping[str, int]) -> None:
    for target, count in replicas.items():
        _patch(target, {"spec": {"replicas": count}})
        print(f"{LOG} {target} restored to {count} replica(s) after its claim was recreated")


def delete_retired() -> None:
    """Delete the objects a deployment rendered before tenants, which an apply does not prune."""
    from scripts.tenant_render import RETIRED

    for kind, namespace, name in RETIRED:
        scope = ["-n", namespace] if namespace else []
        output = run([KUBECTL, *scope, "delete", kind, name, "--ignore-not-found", "--wait=false"])
        if output.strip():
            print(f"{LOG} {output.strip()}")


def diagnose(name: str, namespace: str = PLATFORM_NAMESPACE) -> None:
    """Print the pods, warnings, and recent logs of a deployment that did not roll out.

    The operator has no kubectl, so the task log is where a failed rollout is explained. A
    deployment's log names one pod, which is often the old pod still serving, so the log of each
    pod that is not ready is printed, with its previous container's log after a restart.
    """
    selector = _pod_selector(get_deployment(f"{namespace}/{name}"), name)
    print(f"{LOG} deployment/{name} did not roll out")
    for args in (
        ["get", "pods", "-l", selector, "-o", "wide"],
        ["get", "events", "--field-selector", "type=Warning", "--sort-by=.lastTimestamp"],
        *_unready_pod_logs(selector, namespace),
    ):
        try:
            print(run([KUBECTL, "-n", namespace, *args]), end="")
        except DeployError as exc:
            print(f"{LOG} {exc}")


def _unready_pod_logs(selector: str, namespace: str) -> list[list[str]]:
    try:
        pods = json.loads(
            run([KUBECTL, "-n", namespace, "get", "pods", "-l", selector, "-o", "json"])
        )
    except DeployError as exc:
        print(f"{LOG} {exc}")
        return []
    commands = []
    for pod in pods.get("items", []):
        statuses = (pod.get("status") or {}).get("containerStatuses") or []
        if statuses and all(status.get("ready") for status in statuses):
            continue
        logs = ["logs", f"pod/{pod['metadata']['name']}", "--all-containers", "--tail=60"]
        commands.append(logs)
        if any(status.get("restartCount") for status in statuses):
            commands.append([*logs, "--previous"])
    return commands


def ingress_hostname(name: str, namespace: str = PLATFORM_NAMESPACE) -> str:
    try:
        ingress = json.loads(run([KUBECTL, "-n", namespace, "get", "ingress", name, "-o", "json"]))
    except DeployError:
        return ""
    entries = ingress.get("status", {}).get("loadBalancer", {}).get("ingress") or []
    return entries[0].get("hostname", "") if entries else ""


def wait_for_hostname(
    name: str, namespace: str = PLATFORM_NAMESPACE, attempts: int = 30, delay: int = 10
) -> str:
    for _ in range(attempts):
        hostname = ingress_hostname(name, namespace)
        if hostname:
            return hostname
        time.sleep(delay)
    return ""


def _matches(selector: Mapping[str, Any], labels: Mapping[str, str]) -> bool:
    for key, value in (selector.get("matchLabels") or {}).items():
        if labels.get(key) != value:
            return False
    for expression in selector.get("matchExpressions") or []:
        key, operator = expression["key"], expression["operator"]
        values = expression.get("values") or []
        present = key in labels
        if operator == "In" and not (present and labels[key] in values):
            return False
        if operator == "NotIn" and present and labels[key] in values:
            return False
        if operator == "Exists" and not present:
            return False
        if operator == "DoesNotExist" and present:
            return False
    return True


def unselected_pods(pods: list[dict[str, Any]], policies: list[dict[str, Any]]) -> list[str]:
    """Return `namespace/name` of each pod that is not host-networked and no policy selects."""
    missing = []
    for pod in pods:
        if pod["spec"].get("hostNetwork") or pod["status"].get("phase") in ("Succeeded", "Failed"):
            continue
        namespace = pod["metadata"]["namespace"]
        labels = pod["metadata"].get("labels") or {}
        if not any(
            policy["metadata"]["namespace"] == namespace
            and _matches(policy["spec"].get("podSelector") or {}, labels)
            for policy in policies
        ):
            missing.append(f"{namespace}/{pod['metadata']['name']}")
    return missing


def report_unselected() -> list[str]:
    pods = json.loads(run([KUBECTL, "get", "pods", "-A", "-o", "json"]))["items"]
    policies = json.loads(run([KUBECTL, "get", "networkpolicies", "-A", "-o", "json"]))["items"]
    missing = unselected_pods(pods, policies)
    if missing:
        print(f"{LOG} pods no NetworkPolicy selects, which strict mode would deny:")
        for pod in missing:
            print(f"{LOG}   {pod}")
    else:
        print(f"{LOG} every pod that is not host-networked is selected by a NetworkPolicy")
    return missing


def deploy(inputs: Mapping[str, Any], omit_secrets: list[str]) -> None:
    from scripts.tenant_render import GATEWAY, worker_namespace

    moves = plan_slots(inputs)
    install_controller(inputs)
    documents, skipped = render(overlay_documents(), inputs, ROOT, omit_secrets=omit_secrets)
    for alias in skipped:
        print(f"{LOG} {alias} has no current value, so the workload starts without it")
    start_drains(moves)
    drains = replace_changed_claims(documents)
    recreate_current(moves)
    apply(documents)
    restore_replicas(drains)
    remove_drains(moves)
    delete_retired()
    from scripts.webhook_eks_render import SLACK_POSTER, runs_slack_poster

    platform = [*DEPLOYMENTS, *([SLACK_POSTER] if runs_slack_poster(inputs) else [])]
    rollout(
        [
            *platform,
            *tenant_workers(inputs),
            *tenant_review_workers(inputs),
            *(drain_of(move.worker) for move in moves if move.drain_build is not None),
            *tenant_gateways(inputs),
        ]
    )
    tenants = {
        worker_namespace(entry["name"]): entry["name"] for entry in inputs.get("tenants") or []
    }
    for document in documents:
        namespace = document["metadata"].get("namespace", "")
        if (
            document.get("kind") != "Ingress"
            or document["metadata"]["name"] != GATEWAY
            or namespace not in tenants
        ):
            continue
        tenant = tenants[namespace]
        balancer = wait_for_hostname(GATEWAY, namespace)
        if not balancer:
            raise DeployError(
                f"the {tenant} mcp-gateway load balancer has no host name after five minutes"
            )
        host = document["spec"]["rules"][0]["host"]
        print(f"{LOG} the {tenant} MCP gateway serves https://{host}/mcp from {balancer}")
    report_unselected()
    validate()


def validate() -> None:
    from scripts.validation_job import report_after_rollout

    report_after_rollout([KUBECTL], PLATFORM_NAMESPACE)


def pinned_workflow_ids(build_id: str) -> list[str]:
    """Return the workflow ids of the running executions pinned to ``build_id``.

    The check runs as a Job of the platform image, since this image carries no Temporal client.
    Its exit code follows `scripts.pinned_executions`, which lists each pinned id on an indented
    line after the line that names the build.
    """
    from scripts.validation_job import run_job

    result = run_job([KUBECTL], PLATFORM_NAMESPACE, [build_id], entry=PINNED_ENTRY)
    if result.exit_code == 0:
        return []
    lines = result.report.splitlines()
    if result.exit_code == PINNED_EXIT_CODE:
        header = next((n for n, line in enumerate(lines) if f"pinned to {build_id}:" in line), None)
        listed = [] if header is None else lines[header + 1 :]
        ids = [line.strip() for line in listed if line.startswith("  ") and line.strip()]
        if ids:
            return ids
        raise DeployError(
            f"the pinned execution check of {build_id} exited {PINNED_EXIT_CODE} and listed no "
            f"workflow id in its log: {result.report.strip()}"
        )
    tail = "\n".join(lines[-PINNED_LOG_TAIL_LINES:])
    raise DeployError(
        f"the pinned execution check of {build_id} exited {result.exit_code}:\n{tail}"
    )


@dataclass(frozen=True)
class SlotMove:
    """What a deploy of `build` does to the current and drain slots of one tenant worker."""

    worker: str
    current: dict[str, Any] | None
    build: str
    drain_build: str | None
    copies_current: bool
    had_drain: bool

    @property
    def recreates_current(self) -> bool:
        """Report whether the live current selector lacks the slot label, which no apply adds."""
        from scripts.tenant_render import SLOT_LABEL

        if self.current is None:
            return False
        return SLOT_LABEL not in (self.current["spec"]["selector"].get("matchLabels") or {})

    @property
    def removes_drain(self) -> bool:
        return self.had_drain and self.drain_build is None

    def describe(self) -> str:
        taken = f"{self.worker} takes build {self.build}"
        if self.drain_build is not None:
            drain = drain_of(self.worker)
            return f"{taken}, and {drain} keeps build {self.drain_build} for its pinned runs"
        if self.had_drain:
            return f"{taken}, and {drain_of(self.worker)} is removed, since no run is pinned to it"
        return taken


def _slot_state(deployment: Mapping[str, Any] | None, container: str) -> SlotState:
    """Read a slot from whether its Deployment exists and from the build of its worker image.

    The off-hours sleep scales every slot to zero replicas, and a slot at zero keeps its build and
    the runs pinned to it, so the replica count does not tell whether the slot is in use.
    """
    if deployment is None:
        return SlotState(0, "")
    containers = deployment["spec"]["template"]["spec"]["containers"]
    return SlotState(1, image_build(next(c["image"] for c in containers if c["name"] == container)))


def plan_slots(inputs: Mapping[str, Any]) -> list[SlotMove]:
    """Decide the slots of each tenant worker for the inputs' platform build, changing nothing.

    Raises DeployError, naming each pinned build and its executions, when a worker has no slot
    for the build because both slots hold builds with pinned runs.
    """
    from scripts.tenant_render import CURRENT_SLOT, DRAIN_SLOT

    build = image_build(inputs["images"]["platform"])
    pinned = PinnedRuns(pinned_workflow_ids)
    moves: list[SlotMove] = []
    refusals: list[str] = []
    for worker in [*tenant_workers(inputs), *tenant_review_workers(inputs)]:
        container = _namespaced(worker)[1]
        current, drain = get_deployment(worker), get_deployment(drain_of(worker))
        slots = {
            CURRENT_SLOT: _slot_state(current, container),
            DRAIN_SLOT: _slot_state(drain, container),
        }
        plan = choose_slots(slots, build, pinned)
        if isinstance(plan, SlotRefusal):
            refusals.append(f"{worker} has no slot for build {build}, since {plan.describe()}.")
            continue
        draining = plan.drain is not None and not may_stop(slots[plan.drain], pinned)
        moves.append(
            SlotMove(
                worker,
                current,
                build,
                slots[plan.drain].build if draining else None,
                draining and plan.drain == CURRENT_SLOT,
                drain is not None,
            )
        )
    if refusals:
        raise DeployError(" ".join(["The release stops before any worker moves.", *refusals]))
    return moves


def drain_copy(live: Mapping[str, Any]) -> dict[str, Any]:
    """Return the live current slot of a worker as its drain slot, which serves only pinned runs.

    The copy keeps the name label and the ServiceAccount, so every policy and binding that selects
    the worker selects the drain too.
    """
    from scripts.tenant_render import DRAIN_SLOT, SLOT_ENV, SLOT_LABEL

    metadata = live["metadata"]
    annotations = metadata.get("annotations") or {}
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": drain_of(metadata["name"]),
            "namespace": metadata["namespace"],
            "labels": dict(metadata.get("labels") or {}),
            "annotations": {k: v for k, v in annotations.items() if k not in SERVER_ANNOTATIONS},
        },
        "spec": copy.deepcopy(live["spec"]),
    }
    spec = deployment["spec"]
    spec["selector"]["matchLabels"] = {
        **(spec["selector"].get("matchLabels") or {}),
        SLOT_LABEL: DRAIN_SLOT,
    }
    template = spec["template"]
    template.setdefault("metadata", {}).setdefault("labels", {})[SLOT_LABEL] = DRAIN_SLOT
    worker = next(c for c in template["spec"]["containers"] if c["name"] == metadata["name"])
    worker["env"] = [
        *(variable for variable in worker.get("env", []) if variable["name"] != SLOT_ENV),
        {"name": SLOT_ENV, "value": DRAIN_SLOT},
    ]
    return deployment


def start_drains(moves: Iterable[SlotMove]) -> None:
    """Copy each live current slot that keeps pinned runs into its drain slot and await it."""
    copying = [
        (move, move.current) for move in moves if move.copies_current and move.current is not None
    ]
    if not copying:
        return
    for move, _ in copying:
        print(f"{LOG} {drain_of(move.worker)} takes build {move.drain_build} for its pinned runs")
    apply([drain_copy(current) for _, current in copying])
    rollout([drain_of(move.worker) for move, _ in copying])


def recreate_current(moves: Iterable[SlotMove]) -> None:
    """Delete each current slot whose selector lacks the slot label, for the apply to recreate."""
    for move in moves:
        if not move.recreates_current:
            continue
        namespace, name = _namespaced(move.worker)
        print(f"{LOG} recreating {move.worker}, whose selector gains the slot label")
        seconds = POD_EXIT_SECONDS + termination_grace(move.current)
        run(
            [
                KUBECTL,
                "-n",
                namespace,
                "delete",
                "deployment",
                name,
                "--cascade=foreground",
                "--wait=true",
                f"--timeout={seconds}s",
            ],
            timeout=seconds + 60,
        )


def remove_drains(moves: Iterable[SlotMove]) -> None:
    for move in moves:
        if not move.removes_drain:
            continue
        namespace, name = _namespaced(drain_of(move.worker))
        print(f"{LOG} removing {namespace}/{name}, since no run is pinned to its build")
        run([KUBECTL, "-n", namespace, "delete", "deployment", name, "--ignore-not-found"])


def preflight(inputs: Mapping[str, Any]) -> None:
    for move in plan_slots(inputs):
        print(f"{LOG} {move.describe()}")


def restart(targets: list[str], inputs: Mapping[str, Any]) -> None:
    """Restart each deployment.

    `tenant-workers` names every tenant's activity worker, `tenant-review-workers` every tenant's
    review worker, and `mcp-gateway` every tenant's MCP gateway, since the platform namespace runs
    no gateway and no review worker. A tenant worker's drain slot restarts with it.
    """
    expanded = expand(targets, inputs)
    for target in expanded:
        namespace, name = _namespaced(target)
        run([KUBECTL, "-n", namespace, "rollout", "restart", f"deployment/{name}"])
    rollout(expanded)


def expand(targets: Iterable[str], inputs: Mapping[str, Any]) -> list[str]:
    groups = {
        "tenant-workers": tenant_workers(inputs),
        "tenant-review-workers": tenant_review_workers(inputs),
        "mcp-gateway": tenant_gateways(inputs),
    }
    deployments = [deployment for target in targets for deployment in groups.get(target, [target])]
    return [slot for target in deployments for slot in (target, *_live_drain(target))]


def drain_of(target: str) -> str:
    from scripts.tenant_render import DRAIN_SLOT

    return f"{target}-{DRAIN_SLOT}"


def _live_drain(target: str) -> list[str]:
    """Return the drain slot of a tenant worker when one runs beside it."""
    from scripts.tenant_render import REVIEW_WORKER, WORKER

    if _namespaced(target)[1] not in (WORKER, REVIEW_WORKER):
        return []
    drain = drain_of(target)
    return [drain] if get_deployment(drain) is not None else []


def get_deployment(target: str) -> dict[str, Any] | None:
    namespace, name = _namespaced(target)
    output = run(
        [KUBECTL, "-n", namespace, "get", "deployment", name, "-o", "json", "--ignore-not-found"]
    )
    return json.loads(output) if output.strip() else None


def _patch(target: str, patch: Mapping[str, Any]) -> None:
    namespace, name = _namespaced(target)
    run(
        [
            KUBECTL,
            "-n",
            namespace,
            "patch",
            f"deployment/{name}",
            "--type=merge",
            "-p",
            json.dumps(patch),
        ]
    )


def scale_down(targets: list[str], inputs: Mapping[str, Any]) -> list[str]:
    """Record each running deployment's replica count, scale it to zero, and await its pods.

    A deployment already at zero keeps the count an earlier scale-down recorded. Returns the
    pods still running once POD_EXIT_SECONDS pass.
    """
    deployments = expand(targets, inputs)
    for target in deployments:
        deployment = get_deployment(target)
        if deployment is None:
            print(f"{LOG} {target} does not exist, skipping")
            continue
        replicas = deployment["spec"].get("replicas", 1)
        if replicas:
            _patch(
                target,
                {
                    "metadata": {"annotations": {REPLICAS_ANNOTATION: str(replicas)}},
                    "spec": {"replicas": 0},
                },
            )
            print(f"{LOG} {target} scaled from {replicas} to 0")
    return wait_for_no_pods(deployments)


def scale_up(targets: list[str], inputs: Mapping[str, Any]) -> None:
    """Restore the replica count scale-down recorded on each deployment and await the rollout."""
    restored = []
    for target in expand(targets, inputs):
        deployment = get_deployment(target)
        annotations = (deployment or {}).get("metadata", {}).get("annotations") or {}
        if REPLICAS_ANNOTATION not in annotations:
            continue
        replicas = int(annotations[REPLICAS_ANNOTATION])
        _patch(
            target,
            {
                "metadata": {"annotations": {REPLICAS_ANNOTATION: None}},
                "spec": {"replicas": replicas},
            },
        )
        print(f"{LOG} {target} scaled to {replicas}")
        restored.append(target)
    rollout(restored, SCALE_UP_SECONDS, replaces_pods=False)


def running_pods(targets: Iterable[str]) -> list[str]:
    """Return `namespace/name` of each pod of the deployments that has not finished.

    A terminating pod counts, because its containers keep their connections until they exit.
    """
    running = []
    for target in targets:
        deployment = get_deployment(target)
        if deployment is None:
            continue
        namespace, name = _namespaced(target)
        selector = _pod_selector(deployment, name)
        pods = json.loads(
            run([KUBECTL, "-n", namespace, "get", "pods", "-l", selector, "-o", "json"])
        )
        running.extend(
            f"{namespace}/{pod['metadata']['name']}"
            for pod in pods["items"]
            if pod["status"].get("phase") not in ("Succeeded", "Failed")
        )
    return running


def _pod_selector(deployment: Mapping[str, Any] | None, name: str) -> str:
    """Return the label selector of a deployment's pods, by name label when it does not exist.

    A worker's selector carries its slot, so it resolves only the pods of that slot.
    """
    from scripts.tenant_render import NAME_LABEL

    labels = (
        deployment["spec"]["selector"].get("matchLabels") or {}
        if deployment is not None
        else {NAME_LABEL: name}
    )
    return ",".join(f"{key}={value}" for key, value in sorted(labels.items()))


def wait_for_no_pods(targets: list[str], seconds: int = POD_EXIT_SECONDS) -> list[str]:
    deadline = time.monotonic() + seconds
    while True:
        running = running_pods(targets)
        if not running or time.monotonic() >= deadline:
            return running
        time.sleep(POLL_SECONDS)


def report_running(pods: list[str]) -> bool:
    for pod in pods:
        print(f"{RUNNING_POD}{pod}")
    return bool(pods)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    deploying = commands.add_parser("deploy")
    deploying.add_argument("--omit-secret", action="append", default=[], dest="omit_secrets")
    restarting = commands.add_parser("restart")
    restarting.add_argument("deployments", nargs="+")
    commands.add_parser("check-policies")
    commands.add_parser("preflight")
    for command in ("scale-down", "scale-up", "check-stopped"):
        commands.add_parser(command).add_argument("deployments", nargs="+")
    args = parser.parse_args(argv)

    inputs = json.loads(os.environ["DEPLOY_INPUTS"])
    with tempfile.TemporaryDirectory(prefix="eks-deployer-") as directory:
        kubeconfig = Path(directory) / "kubeconfig"
        write_kubeconfig(inputs["cluster"], kubeconfig)
        os.environ["KUBECONFIG"] = str(kubeconfig)
        try:
            if args.command == "deploy":
                deploy(inputs, args.omit_secrets)
            elif args.command == "preflight":
                preflight(inputs)
            elif args.command == "restart":
                restart(args.deployments, inputs)
            elif args.command == "scale-down":
                report_running(scale_down(args.deployments, inputs))
            elif args.command == "scale-up":
                scale_up(args.deployments, inputs)
            elif args.command == "check-stopped":
                if report_running(running_pods(expand(args.deployments, inputs))):
                    return 1
            elif report_unselected():
                return 1
        except (DeployError, RenderError) as exc:
            print(f"{LOG} {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
