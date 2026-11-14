"""Deploy the in-cluster platform workloads to the local Kubernetes cluster.

The platform, OPA, and LiteLLM images are built through the build command the publisher uses,
loaded into the cluster the way K8S_IMAGE_LOAD names, and rolled out from the rendered
manifests. Every kubectl call names K8S_CONTEXT, so a workstation with several clusters
configured deploys to the one the settings select.
"""

from __future__ import annotations

import argparse
import copy
import json
import platform
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from services import process

from scripts.config import Settings, get_settings

LOG = "[k8s]"
REPO_ROOT = Path(__file__).resolve().parent.parent
WORKER = "activity-worker"
DEPLOYMENTS = ("litellm", "review-worker", "mcp-gateway", "inbound-webhook")
ROLLOUT_TIMEOUT = "600s"
_ARCHES = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "amd64", "amd64": "amd64"}
_PROBES = ("startupProbe", "readinessProbe", "livenessProbe")

Runner = Callable[..., process.CompletedProcess[Any]]


def kubectl(settings: Settings, *args: str) -> list[str]:
    return ["kubectl", "--context", settings.k8s_context, *args]


def namespaced(settings: Settings, *args: str) -> list[str]:
    return kubectl(settings, "--namespace", settings.k8s_namespace, *args)


def tenants(settings: Settings) -> list[str]:
    from services.orchestration.task_queues import tenant_names

    return tenant_names(settings) or ([settings.tenant] if settings.tenant else [])


def in_worker_namespace(settings: Settings, *args: str) -> list[str]:
    """Address the activity worker of TENANT, or of the first tenant TENANTS names."""
    from scripts.tenant_render import worker_namespace

    names = tenants(settings)
    if not names:
        raise SystemExit(f"{LOG} set TENANT or TENANTS, because every activity worker serves one")
    return kubectl(settings, "--namespace", worker_namespace(settings.tenant or names[0]), *args)


def deployments(settings: Settings) -> list[tuple[str, str]]:
    """Return each deployment the local cluster runs, by namespace and name."""
    from scripts.tenant_render import worker_namespace

    return [(settings.k8s_namespace, name) for name in DEPLOYMENTS] + [
        (worker_namespace(name), WORKER) for name in tenants(settings)
    ]


def exec_argv(settings: Settings, command: Sequence[str], *, tty: bool) -> list[str]:
    """Run command in the activity worker container of the running deployment."""
    flags = ["-it"] if tty else ["-i"]
    return in_worker_namespace(
        settings, "exec", *flags, f"deploy/{WORKER}", "-c", WORKER, "--", *command
    )


def one_off_argv(
    settings: Settings, deployment: Mapping[str, Any], command: Sequence[str], name: str
) -> list[str]:
    """Run command in a new pod built from the activity worker's pod template.

    The command runs even when the worker itself does not start, for example while the ticket
    store's remote is wrong, because the pod carries none of the worker's probes.
    """
    spec = copy.deepcopy(deployment["spec"]["template"]["spec"])
    worker = next(c for c in spec["containers"] if c["name"] == WORKER)
    for probe in _PROBES:
        worker.pop(probe, None)
    worker.update({"name": name, "args": list(command), "stdin": True, "stdinOnce": True})
    spec["containers"] = [worker]
    spec["restartPolicy"] = "Never"
    overrides = json.dumps({"apiVersion": "v1", "spec": spec}, separators=(",", ":"))
    return in_worker_namespace(
        settings,
        "run",
        name,
        "--rm",
        "-i",
        "--restart=Never",
        f"--image={worker['image']}",
        f"--overrides={overrides}",
        "--pod-running-timeout=5m",
    )


def run_one_off(settings: Settings, command: Sequence[str], run: Runner = process.run) -> int:
    deployment = json.loads(
        run(
            in_worker_namespace(settings, "get", f"deployment/{WORKER}", "-o", "json"),
            timeout=process.SHORT_COMMAND_TIMEOUT_SECONDS,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    argv = one_off_argv(settings, deployment, command, f"{WORKER}-one-off")
    return run(argv, timeout=process.LONG_COMMAND_TIMEOUT_SECONDS).returncode


def local_arch() -> str:
    machine = platform.machine().lower()
    if machine not in _ARCHES:
        raise SystemExit(f"{LOG} no image platform for the machine architecture {machine}")
    return _ARCHES[machine]


def image_registry(settings: Settings) -> str:
    return settings.k8s_image_load_target if settings.k8s_image_load == "registry" else ""


def image_ref(settings: Settings, name: str, build_id: str) -> str:
    registry = image_registry(settings)
    return f"{registry.rstrip('/')}/{name}:{build_id}" if registry else f"{name}:{build_id}"


def build_images(settings: Settings, build_id: str, run: Runner = process.run) -> list[str]:
    """Build the platform, OPA, and LiteLLM images into the local image store and return them."""
    from scripts.k8s_render import LITELLM_IMAGE, OPA_IMAGE, PLATFORM_IMAGE
    from scripts.publish_platform_images import LITELLM, OPA, PLATFORM, build_locally

    images = [
        (PLATFORM, image_ref(settings, PLATFORM_IMAGE, build_id), {"AGENT_BUILD_ID": build_id}),
        (OPA, image_ref(settings, OPA_IMAGE, build_id), {}),
        (LITELLM, image_ref(settings, LITELLM_IMAGE, build_id), {}),
    ]
    build_locally(images, local_arch(), REPO_ROOT, run)
    return [image for _, image, _ in images]


def load_images(settings: Settings, images: Sequence[str], run: Runner = process.run) -> None:
    """Make the images available to the cluster the way K8S_IMAGE_LOAD names."""
    method, target = settings.k8s_image_load, settings.k8s_image_load_target
    if method == "shared":
        return
    if not target:
        raise SystemExit(f"{LOG} K8S_IMAGE_LOAD={method} needs K8S_IMAGE_LOAD_TARGET")
    for image in images:
        argv = (
            ["kind", "load", "docker-image", image, "--name", target]
            if method == "kind"
            else ["docker", "push", image]
        )
        run(argv, timeout=process.LONG_COMMAND_TIMEOUT_SECONDS, check=True)


def warm_shared_images(
    settings: Settings, images: Sequence[str], run: Runner = process.run
) -> None:
    """Pull each image by tag into every node of a cluster that reads the Docker image store.

    Docker Desktop's nodes reach the Docker image store through a mirror that resolves a tag but
    not a digest, so a pod that names an image by digest finds it only once a node holds it.
    """
    nodes = run(
        kubectl(settings, "get", "nodes", "-o", "jsonpath={.items[*].metadata.name}"),
        timeout=process.SHORT_COMMAND_TIMEOUT_SECONDS,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    for node in nodes:
        for image in images:
            run(
                ["docker", "exec", node, "crictl", "pull", image],
                timeout=process.LONG_COMMAND_TIMEOUT_SECONDS,
                check=True,
            )


def rendered(settings: Settings, build_id: str, compose_files: Sequence[str]) -> list[dict]:
    from dotenv import dotenv_values

    from scripts.cluster_env import compose_config, compose_network
    from scripts.k8s_render import RenderInputs, render

    return render(
        RenderInputs(
            settings=settings,
            env=dotenv_values(REPO_ROOT / ".env"),
            network=compose_network(compose_config(list(compose_files))),
            build_id=build_id,
            image_registry=image_registry(settings),
            root=REPO_ROOT,
        )
    )


def apply(settings: Settings, documents: Sequence[dict], run: Runner = process.run) -> None:
    from scripts.k8s_render import dump

    run(
        kubectl(settings, "apply", "-f", "-"),
        input=dump(documents),
        text=True,
        timeout=process.MEDIUM_COMMAND_TIMEOUT_SECONDS,
        check=True,
    )


def roll(settings: Settings, run: Runner = process.run) -> None:
    """Wait for each deployment to roll out the applied manifests and become ready.

    The renderer stamps each pod template with a digest of the images and generated config,
    so the apply itself rolls a deployment whose inputs changed.
    """
    for namespace, name in deployments(settings):
        run(
            kubectl(
                settings,
                "--namespace",
                namespace,
                "rollout",
                "status",
                f"deployment/{name}",
                f"--timeout={ROLLOUT_TIMEOUT}",
            ),
            timeout=process.LONG_COMMAND_TIMEOUT_SECONDS,
            check=True,
        )


def delete_retired(settings: Settings, run: Runner = process.run) -> None:
    """Delete the objects a deployment rendered before tenants, which an apply does not prune."""
    from scripts.tenant_render import PLATFORM_NAMESPACE, RETIRED, SANDBOX_NAMESPACE

    moved = {
        PLATFORM_NAMESPACE: settings.k8s_namespace,
        SANDBOX_NAMESPACE: settings.openhands_sandbox_namespace,
    }
    for kind, namespace, name in RETIRED:
        if kind == "namespace":
            name = moved.get(name, name)
        scope = ["--namespace", moved.get(namespace, namespace)] if namespace else []
        result = run(
            kubectl(settings, *scope, "delete", kind, name, "--ignore-not-found", "--wait=false"),
            timeout=process.SHORT_COMMAND_TIMEOUT_SECONDS,
            capture_output=True,
            text=True,
        )
        # A workstation cluster serves no SecretProviderClass, so that kind has nothing to delete.
        if result.returncode != 0 and "the server doesn't have a resource type" not in (
            result.stderr or ""
        ):
            raise SystemExit(f"{LOG} could not delete {kind}/{name}: {result.stderr.strip()}")
        if (result.stdout or "").strip():
            print(f"{LOG} {result.stdout.strip()}")


def removable(documents: Sequence[dict], *, volumes: bool) -> list[dict]:
    """Return the resources a delete removes, keeping the namespaces and, unless asked, the PVC."""
    kept = {"Namespace"} if volumes else {"Namespace", "PersistentVolumeClaim"}
    return [document for document in documents if document["kind"] not in kept]


def delete(
    settings: Settings, documents: Sequence[dict], *, volumes: bool, run: Runner = process.run
) -> None:
    from scripts.k8s_render import dump

    run(
        kubectl(settings, "delete", "--ignore-not-found", "-f", "-"),
        input=dump(removable(documents, volumes=volumes)),
        text=True,
        timeout=process.MEDIUM_COMMAND_TIMEOUT_SECONDS,
        check=True,
    )


def scale(settings: Settings, replicas: int, run: Runner = process.run) -> None:
    by_namespace: dict[str, list[str]] = {}
    for namespace, name in deployments(settings):
        by_namespace.setdefault(namespace, []).append(f"deployment/{name}")
    for namespace, targets in by_namespace.items():
        run(
            kubectl(
                settings, "--namespace", namespace, "scale", f"--replicas={replicas}", *targets
            ),
            timeout=process.SHORT_COMMAND_TIMEOUT_SECONDS,
            check=True,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("reload", "deploy", "delete", "stop", "one-off"))
    parser.add_argument("--build-id", default="")
    parser.add_argument("--compose-file", action="append", default=[], dest="compose_files")
    parser.add_argument("--volumes", action="store_true", help="Delete the workspace volume.")
    parser.add_argument("command", nargs="*", help="The one-off command, after --.")
    args = parser.parse_args(argv)

    settings = get_settings()
    if args.action == "stop":
        scale(settings, 0)
        return 0
    if args.action == "one-off":
        return run_one_off(settings, args.command)
    if not args.build_id:
        parser.error(f"{args.action} needs --build-id")
    documents = rendered(settings, args.build_id, args.compose_files)
    if args.action == "delete":
        delete(settings, documents, volumes=args.volumes)
        return 0
    if args.action == "reload":
        images = build_images(settings, args.build_id)
        load_images(settings, images)
    apply(settings, documents)
    delete_retired(settings)
    roll(settings)
    print(f"{LOG} {settings.k8s_context}/{settings.k8s_namespace} runs build {args.build_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
