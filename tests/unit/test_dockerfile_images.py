"""The images a Dockerfile build pulls, read before the build."""

from __future__ import annotations

from pathlib import Path

import pytest
from services.orchestration.gates.dockerfile_images import base_images, dockerfile_for
from services.orchestration.gates.image_admission import ImageRefusedError

DIGEST = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64


def _images(tmp_path: Path, text: str) -> list[str]:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(text, encoding="utf-8")
    return base_images(dockerfile)


def test_every_stage_base_and_copy_and_mount_source_is_collected(tmp_path) -> None:
    images = _images(
        tmp_path,
        f"""# syntax=docker/dockerfile:1
ARG PY=python:3.13-slim@{DIGEST}
FROM --platform=$BUILDPLATFORM ${{PY}} AS build
RUN --mount=type=cache,target=/root/.cache \\
    --mount=type=bind,from=alpine@{OTHER},source=/etc,target=/x pip install .
FROM scratch AS empty
FROM build
COPY --from=build /app /app
COPY --from=0 /app /again
COPY --from=busybox@{OTHER} /bin/sh /bin/sh
""",
    )

    assert images == [f"python:3.13-slim@{DIGEST}", f"alpine@{OTHER}", f"busybox@{OTHER}"]


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("FROM python:3.13-slim\n", "carries no @sha256: digest"),
        (f"FROM python@{DIGEST} AS a\nCOPY --from=node:24 / /\n", "node:24, which carries no"),
        ("ARG PY\nFROM $PY\n", "has no default"),
        (f"FROM python@{DIGEST}\nARG LATE=x@{DIGEST}\nFROM $LATE\n", "has no default"),
        ("ARG PY=x\nFROM ${PY:+y}\n", "cannot resolve"),
        ("FROM\n", "names no image"),
    ],
)
def test_a_reference_the_build_cannot_pin_is_refused(tmp_path, text, reason) -> None:
    with pytest.raises(ImageRefusedError, match=reason):
        _images(tmp_path, text)


def test_a_default_substitution_and_the_escape_directive_resolve(tmp_path) -> None:
    images = _images(
        tmp_path,
        f"# escape=`\nARG PY\nFROM ${{PY:-python@{DIGEST}}} `\n  AS app\nCOPY --from=app / /\n",
    )

    assert images == [f"python@{DIGEST}"]


def test_the_build_reads_the_named_dockerfile_inside_its_context(tmp_path) -> None:
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    (tmp_path / "Containerfile").write_text("FROM scratch\n")
    (tmp_path / "Dockerfile.checks").write_text("FROM scratch\n")

    assert dockerfile_for(tmp_path).name == "Containerfile"
    assert dockerfile_for(tmp_path, "/Dockerfile.checks").name == "Dockerfile.checks"
    with pytest.raises(ImageRefusedError, match="outside the build context"):
        dockerfile_for(tmp_path, "../etc/Dockerfile")
    with pytest.raises(ImageRefusedError, match="does not exist"):
        dockerfile_for(tmp_path, "missing.Dockerfile")
