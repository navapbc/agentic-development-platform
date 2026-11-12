"""Build the example gate engines and push them to the local registry for image-mode gates.

The compose `registry` service is the local stand-in for ECR. The host pushes to it as
`localhost:5001`, and the gate pods pull from it as `host.docker.internal:5001` over plain HTTP,
so the worker needs `GATE_REGISTRY_TLS_VERIFY=false` and the `GATE_ENGINE_IMAGES` value this
writes into .env, each reference pinned by the digest the registry holds.
"""

from __future__ import annotations

import argparse
import json

from scripts.env_file import set_values
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
    values = {
        "GATE_REGISTRY_TLS_VERIFY": "false",
        "GATE_ENGINE_IMAGES": json.dumps(images, separators=(",", ":")),
    }
    set_values(values)
    for key, value in values.items():
        print(f"{LOG} wrote {key}={value} to .env")
    print(f"{LOG} run inv k8s-reload to roll the workers onto them")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
