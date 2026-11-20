"""Build the catalog images, the gate pods' builder, and the scanners for the local cluster.

The kubelet pulls the sandbox images and the builder, so they load the way K8S_IMAGE_LOAD names,
the same as the platform image. Buildah pulls the checks and scanner images inside the gate pods,
so they are pushed to the local registry the gate engines use, which the host reaches as
`localhost:5001` and the pods as `host.docker.internal:5001`. The command writes
OPENHANDS_AGENT_SERVER_IMAGE, the agent-server-python-node image, GATE_ENGINE_BUILDER_IMAGE,
RUNTIME_IMAGES, which makes catalog:agent-server-jvm, catalog:agent-server-go, and the
catalog:checks-* images resolve, SEMGREP_IMAGE, and TRIVY_IMAGE into .env, each reference pinned
by its digest.
"""

from __future__ import annotations

import argparse
import json

from services import process

LOG = "[runtime-images]"
BUILDER = "agentic-dev-platform-buildah"
SEMGREP_IMAGE = "agentic-dev-platform-semgrep"
TRIVY_IMAGE = "agentic-dev-platform-trivy"


def local_digest(image: str, settings) -> str:
    """Return the digest the cluster resolves `image` by.

    A registry load reads the digest the registry holds. Otherwise the cluster shares the image
    store, where an image's ID is the digest of its index.
    """
    from scripts.publish_platform_images import registry_digest

    if settings.k8s_image_load == "registry":
        return registry_digest(image)
    return process.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image],
        timeout=process.SHORT_COMMAND_TIMEOUT_SECONDS,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", default="localhost:5001", help="Registry the host pushes to.")
    parser.add_argument(
        "--pull-registry",
        default="host.docker.internal:5001",
        help="The same registry as the gate pods address it.",
    )
    args = parser.parse_args(argv)

    from scripts.build_id import release_version
    from scripts.config import get_settings
    from scripts.env_file import set_values
    from scripts.gate_engine_images import pull_references
    from scripts.k8s_cluster import (
        REPO_ROOT,
        image_ref,
        load_images,
        local_arch,
        warm_shared_images,
    )
    from scripts.publish_platform_images import (
        AGENT_SERVER,
        BUILDAH,
        RUNTIME_IMAGES,
        SEMGREP,
        TRIVY,
        build_context,
        build_locally,
        content_hash,
        push_locally,
    )

    settings = get_settings()
    arch = local_arch()
    kubelet_specs = {
        **{name: spec for name, spec in RUNTIME_IMAGES.items() if name.startswith("agent-server")},
        AGENT_SERVER.name: AGENT_SERVER,
        BUILDER: BUILDAH,
    }
    images = {
        name: image_ref(settings, name, f"{release_version()}-{content_hash(spec, REPO_ROOT)}")
        for name, spec in kubelet_specs.items()
    }
    build_locally([(kubelet_specs[name], image, {}) for name, image in images.items()], arch)
    load_images(settings, list(images.values()))
    if settings.k8s_image_load == "shared":
        warm_shared_images(settings, list(images.values()))
    pinned = {name: f"{image}@{local_digest(image, settings)}" for name, image in images.items()}

    buildah_specs = {
        **{name: spec for name, spec in RUNTIME_IMAGES.items() if name.startswith("checks-")},
        SEMGREP_IMAGE: SEMGREP,
        TRIVY_IMAGE: TRIVY,
    }
    with build_context(REPO_ROOT) as context:
        pushed = push_locally(
            {name: (spec, context) for name, spec in buildah_specs.items()}, args.registry, arch
        )
    pinned.update(pull_references(pushed, args.registry, args.pull_registry))

    builder = pinned.pop(BUILDER)
    agent_server = pinned.pop(AGENT_SERVER.name)
    semgrep = pinned.pop(SEMGREP_IMAGE)
    trivy = pinned.pop(TRIVY_IMAGE)
    values = {
        "RUNTIME_IMAGES": json.dumps(pinned, separators=(",", ":")),
        "OPENHANDS_AGENT_SERVER_IMAGE": agent_server,
        "GATE_ENGINE_BUILDER_IMAGE": builder,
        "SEMGREP_IMAGE": semgrep,
        "TRIVY_IMAGE": trivy,
        "GATE_REGISTRY_TLS_VERIFY": "false",
    }
    set_values(values)
    for key, value in values.items():
        print(f"{LOG} wrote {key}={value} to .env")
    print(f"{LOG} run inv k8s-reload to roll the workers onto them")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
