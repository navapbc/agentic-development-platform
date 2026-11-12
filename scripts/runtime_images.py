"""Build the catalog sandbox images and the gate pods' builder for the local cluster.

The images load the way K8S_IMAGE_LOAD names, the same as the platform image. The command writes
RUNTIME_IMAGES, which makes catalog:agent-server-jvm and catalog:agent-server-go resolve to them,
and GATE_ENGINE_BUILDER_IMAGE into .env, each reference pinned by its digest.
"""

from __future__ import annotations

import argparse
import json

from services import process

LOG = "[runtime-images]"
BUILDER = "agentic-dev-platform-buildah"


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
    argparse.ArgumentParser(description=__doc__).parse_args(argv)

    from scripts.build_id import release_version
    from scripts.config import get_settings
    from scripts.env_file import set_values
    from scripts.k8s_cluster import (
        REPO_ROOT,
        image_ref,
        load_images,
        local_arch,
        warm_shared_images,
    )
    from scripts.publish_platform_images import (
        BUILDAH,
        RUNTIME_IMAGES,
        build_locally,
        content_hash,
    )

    settings = get_settings()
    specs = {**RUNTIME_IMAGES, BUILDER: BUILDAH}
    images = {
        name: image_ref(settings, name, f"{release_version()}-{content_hash(spec, REPO_ROOT)}")
        for name, spec in specs.items()
    }
    build_locally(
        [(specs[name], image, {}) for name, image in images.items()], local_arch(), REPO_ROOT
    )
    load_images(settings, list(images.values()))
    if settings.k8s_image_load == "shared":
        warm_shared_images(settings, list(images.values()))
    pinned = {name: f"{image}@{local_digest(image, settings)}" for name, image in images.items()}
    builder = pinned.pop(BUILDER)
    values = {
        "RUNTIME_IMAGES": json.dumps(pinned, separators=(",", ":")),
        "GATE_ENGINE_BUILDER_IMAGE": builder,
    }
    set_values(values)
    for key, value in values.items():
        print(f"{LOG} wrote {key}={value} to .env")
    print(f"{LOG} run inv k8s-reload to roll the workers onto them")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
