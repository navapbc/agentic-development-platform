"""Deploy the EKS workloads from inside the VPC, or restart them after a credential rotation.

This is the entrypoint of the eks-deployer image, which `inv eks-deploy` and the secret store's
restart rule start as a one-shot ECS task in the management tier. The task definition carries the
environment's values in DEPLOY_INPUTS, and the image carries kubectl, helm, the
aws-iam-authenticator token helper, the vendored AWS Load Balancer Controller chart, and the AWS
overlay, so the workstation needs none of them.

- `deploy` installs or upgrades the Load Balancer Controller, renders the overlay for the
  environment, applies it, waits for each deployment, and sets the MCP gateway's host list once
  its load balancer exists. It ends by listing every pod that is not host-networked and that no
  NetworkPolicy selects, which strict network policy mode would deny.
- `restart` rolls the named deployments, `namespace/name` or a platform deployment by name, and
  `tenant-workers` for every tenant's activity worker, so their pods read the current secret values.
- `check-policies` prints the pods no NetworkPolicy selects and exits 1 when there are any.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from services import process

from scripts.eks_render import PLATFORM_NAMESPACE, RenderError, render

KUBECTL = "/usr/local/bin/kubectl"
HELM = "/usr/local/bin/helm"
AUTHENTICATOR = "/usr/local/bin/aws-iam-authenticator"
ROOT = Path(os.environ.get("EKS_DEPLOYER_ROOT", "/opt/eks-deployer"))
OVERLAY = ROOT / "deploy" / "kubernetes" / "aws"
CHART = ROOT / "charts" / "aws-load-balancer-controller.tgz"
DEPLOYMENTS = ("litellm", "inbound-webhook", "mcp-gateway")
COMMAND_TIMEOUT = 300
ROLLOUT_TIMEOUT = "10m"
LOG = "[eks-deployer]"


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
    listing = {"apiVersion": "v1", "kind": "List", "items": documents}
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


def rollout(targets: Iterable[str]) -> None:
    for target in targets:
        namespace, name = _namespaced(target)
        try:
            run(
                [
                    KUBECTL,
                    "-n",
                    namespace,
                    "rollout",
                    "status",
                    f"deployment/{name}",
                    f"--timeout={ROLLOUT_TIMEOUT}",
                ],
                timeout=900,
            )
        except DeployError:
            diagnose(name, namespace)
            raise
        print(f"{LOG} {namespace}/deployment/{name} is rolled out")


def delete_retired() -> None:
    """Delete the objects a deployment rendered before tenants, which an apply does not prune."""
    from scripts.tenant_render import RETIRED

    for kind, namespace, name in RETIRED:
        scope = ["-n", namespace] if namespace else []
        output = run([KUBECTL, *scope, "delete", kind, name, "--ignore-not-found", "--wait=false"])
        if output.strip():
            print(f"{LOG} {output.strip()}")


def diagnose(name: str, namespace: str = PLATFORM_NAMESPACE) -> None:
    """Print the pods, warnings, and recent log of a deployment that did not roll out.

    The operator has no kubectl, so the task log is where a failed rollout is explained.
    """
    selector = f"app.kubernetes.io/name={name}"
    print(f"{LOG} deployment/{name} did not roll out")
    for args in (
        ["get", "pods", "-l", selector, "-o", "wide"],
        ["get", "events", "--field-selector", "type=Warning", "--sort-by=.lastTimestamp"],
        ["logs", f"deployment/{name}", "--all-containers", "--tail=40"],
    ):
        try:
            print(run([KUBECTL, "-n", namespace, *args]), end="")
        except DeployError as exc:
            print(f"{LOG} {exc}")


def ingress_hostname(name: str) -> str:
    try:
        ingress = json.loads(
            run([KUBECTL, "-n", PLATFORM_NAMESPACE, "get", "ingress", name, "-o", "json"])
        )
    except DeployError:
        return ""
    entries = ingress.get("status", {}).get("loadBalancer", {}).get("ingress") or []
    return entries[0].get("hostname", "") if entries else ""


def wait_for_hostname(name: str, attempts: int = 30, delay: int = 10) -> str:
    for _ in range(attempts):
        hostname = ingress_hostname(name)
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
    install_controller(inputs)
    documents, skipped = render(
        overlay_documents(),
        inputs,
        ROOT,
        omit_secrets=omit_secrets,
        mcp_hostname=ingress_hostname("mcp-gateway"),
    )
    for alias in skipped:
        print(f"{LOG} {alias} has no current value, so the workload starts without it")
    apply(documents)
    delete_retired()
    rollout([*DEPLOYMENTS, *tenant_workers(inputs)])
    if any(
        d.get("kind") == "Ingress" and d["metadata"]["name"] == "mcp-gateway" for d in documents
    ):
        hostname = wait_for_hostname("mcp-gateway")
        if not hostname:
            raise DeployError("the mcp-gateway load balancer has no host name after five minutes")
        documents, _ = render(
            overlay_documents(), inputs, ROOT, omit_secrets=omit_secrets, mcp_hostname=hostname
        )
        apply(documents)
        rollout(["mcp-gateway"])
        print(f"{LOG} the MCP gateway accepts the host {hostname}")
    report_unselected()


def restart(targets: list[str], inputs: Mapping[str, Any]) -> None:
    """Restart each deployment, where `tenant-workers` names every tenant's activity worker."""
    expanded = [
        worker
        for target in targets
        for worker in (tenant_workers(inputs) if target == "tenant-workers" else [target])
    ]
    for target in expanded:
        namespace, name = _namespaced(target)
        run([KUBECTL, "-n", namespace, "rollout", "restart", f"deployment/{name}"])
    rollout(expanded)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    deploying = commands.add_parser("deploy")
    deploying.add_argument("--omit-secret", action="append", default=[], dest="omit_secrets")
    restarting = commands.add_parser("restart")
    restarting.add_argument("deployments", nargs="+")
    commands.add_parser("check-policies")
    args = parser.parse_args(argv)

    inputs = json.loads(os.environ["DEPLOY_INPUTS"])
    with tempfile.TemporaryDirectory(prefix="eks-deployer-") as directory:
        kubeconfig = Path(directory) / "kubeconfig"
        write_kubeconfig(inputs["cluster"], kubeconfig)
        os.environ["KUBECONFIG"] = str(kubeconfig)
        try:
            if args.command == "deploy":
                deploy(inputs, args.omit_secrets)
            elif args.command == "restart":
                restart(args.deployments, inputs)
            elif report_unselected():
                return 1
        except (DeployError, RenderError) as exc:
            print(f"{LOG} {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
