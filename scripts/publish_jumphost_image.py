"""Build the jumphost-base image on the hardened base, scan it, push it, and sign it.

The image goes through the Publisher and the Signer that `scripts.publish_platform_images` uses.
It builds from the hardened base that `scripts.al2023_base` describes, and is scanned with the
Amazon Inspector SBOM generator and the Inspector Scan API, as `scripts.image_scan` describes, and
against the CIS benchmark before it is pushed. The results of each CIS scan are written under
`--cis-evidence`. An image with a finding at `--block-severity` or above that the platform image
exceptions do not accept, or a CIS rule that neither passes nor is accepted, is not pushed, and the
run exits 1.

The pushed image is signed with the platform signing profile through notation and the AWS Signer
plugin, run from the buildah builder image that `inv publish-platform-images` publishes, on the
architecture its tag names. Only a digest this run built, scanned, and pushed is signed. The
repository holds immutable tags, so a tag it already holds is not rebuilt. The run accepts such a
tag when it already carries a signature, and otherwise refuses it, because nothing shows the image
was scanned.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

from scripts.publish_platform_images import (
    BUILDAH,
    IMAGES_FILE,
    REPO_ROOT,
    TFVARS_KEYS,
    Publisher,
    PublishError,
    Signer,
    build_context,
    signing_profile,
)
from scripts.publish_temporal_images import PLATFORMS, ImageSpec

LOG = "[publish-jumphost-image]"
DEFAULT_REPOSITORY = "agentic-platform/jumphost-base"
JUMPHOST = ImageSpec(
    "jumphost-base",
    "docker/jumphost-base/Dockerfile",
    ("docker/jumphost-base/entrypoint.sh", "LICENSE", "NOTICE"),
)


def image_spec(repository: str) -> tuple[str, ImageSpec]:
    """Return the name prefix and the image spec that publish to `repository`."""
    name_prefix, _, name = repository.rpartition("/")
    return name_prefix, replace(JUMPHOST, name=name)


def signing_builder(explicit: str, images_file: Path | None = None) -> str:
    """Return the builder image to sign in, from the flag or the published buildah reference."""
    if explicit:
        return explicit
    try:
        references = json.loads((images_file or IMAGES_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError, json.JSONDecodeError:
        return ""
    return references.get(TFVARS_KEYS[BUILDAH.name], "")


def builder_arch(builder: str) -> str:
    """Return the architecture that ends the builder's tag, as the platform publish tags images."""
    tag = builder.split("@", 1)[0].rpartition("/")[2].partition(":")[2]
    return next((arch for arch in PLATFORMS if tag.endswith(f"-{arch}")), "")


def make_signer(session, ecr, registry: str, profile: str, builder: str, run=None) -> Signer:
    """Return a signer that runs the builder on its own architecture."""
    return Signer(session, ecr, registry, profile, builder_arch(builder), run)


def _existing_tag_signed(
    publisher: Publisher, signer: Signer | None, reference: str, builder: str
) -> bool:
    if signer is None:
        return False
    publisher._login()
    return signer.signed(reference, builder)


def publish_signed(
    publisher: Publisher,
    signer: Signer | None,
    spec: ImageSpec,
    tag: str,
    builder: str,
    root: Path = REPO_ROOT,
) -> int:
    """Push the image unless a scan refuses it, then sign it, returning the exit status.

    A tag the repository already holds is accepted only when it carries a signature.
    """
    with build_context(root) as context:
        reference = publisher.tagged(spec, context, tag)
    if publisher.refused:
        for refusal in publisher.refused:
            print(f"{publisher.log} not pushed: {refusal}")
        print(
            f"{publisher.log} the image carries blocking findings and was not pushed. Rebuild it "
            "on a patched base, or accept each finding in the exceptions file with a reason and "
            "an expiry."
        )
        return 1
    try:
        if reference not in publisher.pushed:
            if not _existing_tag_signed(publisher, signer, reference, builder):
                print(
                    f"{publisher.log} {reference} was pushed before this run, and no signature "
                    "on it shows that it was scanned. Publish under a new --tag and set "
                    "jumphost_image_tag to it."
                )
                return 1
            print(f"{publisher.log} {reference} is already pushed and signed")
        elif signer is None:
            print(f"{publisher.log} --unsigned, so {reference} is not signed")
        else:
            publisher._login()
            signer.sign([reference], builder, catalog={spec.name})
    except PublishError as exc:
        print(f"{publisher.log} {exc}")
        return 1
    print(reference)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tag",
        required=True,
        help="Image tag to push, a tag the repository does not hold yet, and jumphost_image_tag.",
    )
    parser.add_argument("--arch", choices=sorted(PLATFORMS), default="arm64")
    parser.add_argument("--repository", default=DEFAULT_REPOSITORY, help="ECR repository name.")
    parser.add_argument("--region", default="", help="Registry region. Defaults to AWS_REGION.")
    parser.add_argument(
        "--block-severity",
        default="high",
        choices=("low", "medium", "high", "critical"),
        help="Lowest Inspector severity that keeps the image from being pushed.",
    )
    parser.add_argument(
        "--exceptions",
        default="",
        help="A local image_exceptions.yaml to read in place of the platform exceptions file.",
    )
    parser.add_argument(
        "--cis-evidence",
        type=Path,
        default=Path("cis-evidence"),
        help="Directory for the CIS benchmark scan results of the base and of the image.",
    )
    parser.add_argument(
        "--signing-profile",
        default="",
        help=(
            "AWS Signer profile ARN to sign the image with. Defaults to IMAGE_SIGNING_PROFILE_ARN, "
            "then the composition root's image_signing_profile_arn."
        ),
    )
    parser.add_argument(
        "--builder",
        default="",
        help=(
            "Image that runs notation for the signing. Defaults to the eks_buildah_image "
            f"reference in {IMAGES_FILE.name}."
        ),
    )
    parser.add_argument(
        "--unsigned",
        action="store_true",
        help="Publish without signing, for a deployment that has no platform signing profile.",
    )
    args = parser.parse_args(argv)

    profile = "" if args.unsigned else signing_profile(args.signing_profile)
    builder = signing_builder(args.builder) if profile else ""
    if not profile and not args.unsigned:
        print(
            f"{LOG} no signing profile, so nothing is published. Set IMAGE_SIGNING_PROFILE_ARN or "
            "--signing-profile to the image_signing_profile_arn output, or pass --unsigned for a "
            "deployment that has no platform signing profile."
        )
        return 1
    if profile and not builder:
        print(
            f"{LOG} no builder image to sign in, so nothing is published. Run inv "
            "publish-platform-images first, or pass --builder."
        )
        return 1
    if profile and not builder_arch(builder):
        print(
            f"{LOG} the tag of the builder image {builder} names no architecture, so nothing is "
            "published. Pass a builder whose tag ends in -arm64 or -amd64."
        )
        return 1

    from scripts.aws_clients import configure_aws_fips
    from scripts.config import get_settings

    settings = get_settings()
    configure_aws_fips(settings)

    import boto3
    from services.integration.image_exceptions import platform_exceptions

    from scripts.al2023_base import Base
    from scripts.image_scan import ImageScanner

    region = args.region or settings.aws_region
    session = boto3.Session(region_name=region)
    account = session.client("sts").get_caller_identity()["Account"]
    registry = f"{account}.dkr.ecr.{region}.amazonaws.com"
    ecr = session.client("ecr")
    scanner = ImageScanner(
        session,
        args.arch,
        block=args.block_severity,
        exceptions=platform_exceptions(settings, args.exceptions),
    )
    name_prefix, spec = image_spec(args.repository)
    publisher = Publisher(
        ecr,
        registry,
        name_prefix,
        args.arch,
        scanner,
        log=LOG,
        base=Base(args.arch, evidence=args.cis_evidence),
    )
    signer = make_signer(session, ecr, registry, profile, builder) if profile else None
    return publish_signed(publisher, signer, spec, args.tag, builder)


if __name__ == "__main__":
    raise SystemExit(main())
