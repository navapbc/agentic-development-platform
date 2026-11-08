"""Apply the composition root with the published image references, then deploy the services.

`inv publish-platform-images`, or the `publish-images` workflow artifact, writes the references
to `images.auto.tfvars.json` in the composition root, which Terraform loads with no flag. This
plans the root, applies the plan after confirmation, and runs `inv platform-deploy`, which rolls
the services onto the task definitions the apply registered, then `inv eks-deploy` when the root
has an EKS cluster.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from services import process

from scripts.publish_platform_images import IMAGES_FILE, TFVARS_KEYS

LOG = "[aws-release]"
ROOT = IMAGES_FILE.parent
PLAN = "release.tfplan"
NO_CHANGES, CHANGES = 0, 2

Runner = Callable[..., process.CompletedProcess[Any]]


class ReleaseError(Exception):
    """The release cannot proceed."""


def read_images(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise ReleaseError(
            f"{path} is missing. Run inv publish-platform-images, or download the images "
            "artifact of a publish-images workflow run into the composition root."
        )
    images = json.loads(path.read_text(encoding="utf-8"))
    missing = sorted(set(TFVARS_KEYS.values()) - set(images))
    if missing:
        raise ReleaseError(f"{path} names no {', '.join(missing)}. Publish every image first.")
    return images


def shadowing_variables(var_file: Path, images: dict[str, str]) -> list[str]:
    """Return the image variables the var file also sets, whose values would win over the file.

    Terraform applies a -var-file after the *.auto.tfvars.json files, so an image variable left
    in the var file silently replaces the published reference.
    """
    text = var_file.read_text(encoding="utf-8")
    return sorted(
        name
        for name in images
        if any(line.split("=", 1)[0].strip() == name for line in text.splitlines())
    )


def plan(var_file: Path, run: Runner = process.run) -> int:
    result = run(
        [
            "terraform",
            f"-chdir={ROOT}",
            "plan",
            "-input=false",
            "-detailed-exitcode",
            f"-var-file={var_file.name}",
            f"-out={PLAN}",
        ],
        timeout=process.LONG_COMMAND_TIMEOUT_SECONDS,
    )
    if result.returncode not in (NO_CHANGES, CHANGES):
        raise ReleaseError(f"terraform plan exited {result.returncode}")
    return result.returncode


def apply(run: Runner = process.run) -> None:
    run(
        ["terraform", f"-chdir={ROOT}", "apply", "-input=false", PLAN],
        timeout=process.LONG_COMMAND_TIMEOUT_SECONDS,
        check=True,
    )


def release(
    var_file: Path,
    *,
    confirm: Callable[[], bool],
    deploy: Callable[[], int],
    run: Runner = process.run,
) -> int:
    images = read_images(IMAGES_FILE)
    shadowed = shadowing_variables(var_file, images)
    if shadowed:
        raise ReleaseError(
            f"{var_file.name} sets {', '.join(shadowed)}, which would replace the published "
            f"references in {IMAGES_FILE.name}. Remove them from {var_file.name}."
        )
    try:
        if plan(var_file, run) == CHANGES:
            if not confirm():
                print(f"{LOG} not applied")
                return 1
            apply(run)
        else:
            print(f"{LOG} the composition root already matches, nothing to apply")
    finally:
        (ROOT / PLAN).unlink(missing_ok=True)
    return deploy()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--var-file", default="greenfield.tfvars", help="In the composition root.")
    parser.add_argument(
        "--region", default="", help="Region for the deploy. Defaults to AWS_REGION."
    )
    parser.add_argument("--yes", action="store_true", help="Apply without asking.")
    args = parser.parse_args(argv)

    from scripts.eks_deploy import main as eks_deploy
    from scripts.platform_deploy import main as platform_deploy

    def confirm() -> bool:
        if args.yes:
            return True
        return input(f"{LOG} apply this plan? [y/N] ").strip().lower() == "y"

    deploy_args = ["--region", args.region] if args.region else []
    try:
        return release(
            ROOT / args.var_file,
            confirm=confirm,
            deploy=lambda: platform_deploy(deploy_args) or eks_deploy(deploy_args),
        )
    except ReleaseError as exc:
        print(f"{LOG} {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
