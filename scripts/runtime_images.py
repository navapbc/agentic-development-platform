"""Build the catalog sandbox images into the local cluster's image store for runtime profiles.

The images load the way K8S_IMAGE_LOAD names, the same as the platform image, and the command
prints the RUNTIME_IMAGES value that makes catalog:agent-server-jvm and catalog:agent-server-go
resolve to them.
"""

from __future__ import annotations

import argparse
import json

LOG = "[runtime-images]"


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)

    from scripts.build_id import release_version
    from scripts.config import get_settings
    from scripts.k8s_cluster import REPO_ROOT, image_ref, load_images, local_arch
    from scripts.publish_platform_images import RUNTIME_IMAGES, build_locally, content_hash

    settings = get_settings()
    images = {
        name: image_ref(settings, name, f"{release_version()}-{content_hash(spec, REPO_ROOT)}")
        for name, spec in RUNTIME_IMAGES.items()
    }
    build_locally(
        [(RUNTIME_IMAGES[name], image, {}) for name, image in images.items()],
        local_arch(),
        REPO_ROOT,
    )
    load_images(settings, list(images.values()))
    print(f"{LOG} set this in .env, then run inv k8s-reload:")
    print(f"RUNTIME_IMAGES={json.dumps(images, separators=(',', ':'))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
