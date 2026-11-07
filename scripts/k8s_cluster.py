"""Deploy the in-cluster platform workloads to the local Kubernetes cluster.

The platform and OPA images are built through the build command the image publisher uses,
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
DEPLOYMENTS = ("litellm", "activity-worker", "review-worker", "mcp-gateway", "inbound-webhook")
ROLLOUT_TIMEOUT = "600s"
_ARCHES = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "amd64", "amd64": "amd64"}
_PROBES = ("startupProbe", "readinessProbe", "livenessProbe")

Runner = Callable[..., process.CompletedProcess[Any]]


def kubectl(settings: Settings, *args: str) -> list[str]:
    return ["kubectl", "--context", settings.k8s_context, *args]


def namespaced(settings: Settings, *args: str) -> list[str]:
    return kubectl(settings, "--namespace", settings.k8s_namespace, *args)


def exec_argv(settings: Settings, command: Sequence[str], *, tty: bool) -> list[str]:
    """Run command in the activity worker container of the running deployment."""
    flags = ["-it"] if tty else ["-i"]
    return namespaced(settings, "exec", *flags, f"deploy/{WORKER}", "-c", WORKER, "--", *command)


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
    worker.update({"name": name, "command": list(command), "stdin": True, "stdinOnce": True})
    worker.pop("args", None)
    spec["containers"] = [worker]
    spec["restartPolicy"] = "Never"
    overrides = json.dumps({"apiVersion": "v1", "spec": spec}, separators=(",", ":"))
    return namespaced(
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
            namespaced(settings, "get", f"deployment/{WORKER}", "-o", "json"),
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


def build_images(
    settings: Settings, build_id: str, env: Mapping[str, str | None], run: Runner = process.run
) -> list[str]:
    """Build the platform and OPA images into the local image store, returning their refs."""
    from scripts.k8s_render import OPA_IMAGE, PLATFORM_IMAGE
    from scripts.publish_platform_images import OPA, PLATFORM, AcliBuild, build_context
    from scripts.publish_temporal_images import BUILD_TIMEOUT_SECONDS, build_command

    acli = AcliBuild(
        (env.get("INSTALL_ACLI") or "false").lower() == "true",
        env.get("ACLI_VERSION") or "",
        env.get("ACLI_SHA256") or "",
    )
    arch = local_arch()
    built = []
    with build_context(REPO_ROOT) as context:
        for spec, name, args in (
            (PLATFORM, PLATFORM_IMAGE, {"AGENT_BUILD_ID": build_id, **acli.build_args()}),
            (OPA, OPA_IMAGE, {}),
        ):
            image = image_ref(settings, name, build_id)
            print(f"{LOG} building {image}")
            run(
                build_command(spec, context, image, arch, args, output="--load"),
                timeout=BUILD_TIMEOUT_SECONDS,
                check=True,
            )
            built.append(image)
    return built


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
    for target in (f"deployment/{name}" for name in DEPLOYMENTS):
        run(
            namespaced(settings, "rollout", "status", target, f"--timeout={ROLLOUT_TIMEOUT}"),
            timeout=process.LONG_COMMAND_TIMEOUT_SECONDS,
            check=True,
        )


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
    run(
        namespaced(
            settings,
            "scale",
            f"--replicas={replicas}",
            *(f"deployment/{name}" for name in DEPLOYMENTS),
        ),
        timeout=process.SHORT_COMMAND_TIMEOUT_SECONDS,
        check=True,
    )


def main(argv: list[str] | None = None) -> int:
    from dotenv import dotenv_values

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
        images = build_images(settings, args.build_id, dotenv_values(REPO_ROOT / ".env"))
        load_images(settings, images)
    apply(settings, documents)
    roll(settings)
    print(f"{LOG} {settings.k8s_context}/{settings.k8s_namespace} runs build {args.build_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
