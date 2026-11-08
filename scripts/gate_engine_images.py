"""Build the example gate engines and push them to the local registry for image-mode gates.

The compose `registry` service is the local stand-in for ECR. The host pushes to it as
`localhost:5001`, and the gate pods pull from it as `host.docker.internal:5001` over plain HTTP,
so the worker needs `GATE_REGISTRY_TLS_VERIFY=false` and the `GATE_ENGINE_IMAGES` value this
prints.
"""

from __future__ import annotations

import argparse
import json

from scripts.k8s_cluster import local_arch
from scripts.publish_platform_images import push_engines_locally

LOG = "[gate-engine-images]"


def pull_references(pushed: dict[str, str], push_registry: str, pull_registry: str) -> dict:
    return {
        name: pull_registry + image.removeprefix(push_registry) for name, image in pushed.items()
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", default="localhost:5001", help="Registry the host pushes to.")
    parser.add_argument(
        "--pull-registry",
        default="host.docker.internal:5001",
        help="The same registry as the gate pods address it.",
    )
    args = parser.parse_args(argv)
    pushed = push_engines_locally(args.registry, local_arch())
    images = pull_references(pushed, args.registry, args.pull_registry)
    print(f"{LOG} set these in .env, then run inv k8s-reload:")
    print("GATE_REGISTRY_TLS_VERIFY=false")
    print(f"GATE_ENGINE_IMAGES={json.dumps(images, separators=(',', ':'))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
