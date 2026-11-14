"""The upstream release check compares each pin with its newest release without the network."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from scripts import upstream_releases
from scripts.upstream_releases import Component, Release, Scan

DIGEST = "sha256:" + "a" * 64
PINS = {
    "GO_VERSION": "1.27.4",
    "KUBECTL_MODE": "source",
    "KUBECTL_VERSION": "v1.37.2",
    "TRIVY_MODE": "source",
    "TRIVY_VERSION": "v0.76.2",
    "TRIVY_IMAGE_MODE": "build",
    "TRIVY_UPSTREAM_IMAGE": f"docker.io/aquasec/trivy:0.76.2@{DIGEST}",
    "LITELLM_VERSION": "v1.109.1",
    "LITELLM_IMAGE_MODE": "release",
    "LITELLM_UPSTREAM_IMAGE": f"ghcr.io/berriai/litellm:v1.109.1@{DIGEST}",
    "UV_IMAGE": f"ghcr.io/astral-sh/uv:0.13.8@{DIGEST}",
}
COMPONENTS = (
    Component("go", upstream_releases.GO_FEED, "GO_VERSION"),
    Component("kubectl", "kubernetes/kubernetes", "KUBECTL_VERSION", "KUBECTL"),
    Component("trivy", "aquasecurity/trivy", "TRIVY_VERSION", "TRIVY"),
    Component("litellm", "BerriAI/litellm", "LITELLM_VERSION", "LITELLM"),
    Component("uv", "astral-sh/uv", "UV_IMAGE"),
    Component("helm", "helm/helm", "HELM_VERSION", "HELM"),
)


def fetcher(documents: dict[str, Any]):
    def fetch(url: str) -> Any:
        document = documents[url]
        if isinstance(document, Exception):
            raise document
        return document

    return fetch


def github(repository: str, tag: str) -> tuple[str, dict[str, str]]:
    return (
        f"{upstream_releases.GITHUB_API}/repos/{repository}/releases/latest",
        {"tag_name": tag, "html_url": f"https://github.com/{repository}/releases/tag/{tag}"},
    )


DOCUMENTS: dict[str, Any] = dict(
    [
        github("kubernetes/kubernetes", "v1.37.3"),
        github("aquasecurity/trivy", "v0.77.0"),
        github("BerriAI/litellm", "v1.109.1"),
        github("astral-sh/uv", "0.14.0"),
    ]
)
DOCUMENTS[upstream_releases.GO_DOWNLOADS] = [
    {"version": "go1.28rc1", "stable": False},
    {"version": "go1.27.5", "stable": True},
]


def by_name(releases: list[Release]) -> dict[str, Release]:
    return {release.component: release for release in releases}


def test_each_pin_is_compared_with_its_newest_release() -> None:
    releases = by_name(upstream_releases.check(PINS, fetcher(DOCUMENTS), COMPONENTS))

    assert "helm" not in releases
    assert (releases["go"].newest, releases["go"].target, releases["go"].newer) == (
        "go1.27.5",
        "1.27.5",
        True,
    )
    assert releases["go"].advice() == (
        "A newer release exists. Raise GO_VERSION to 1.27.5 and refresh the checksums or digest "
        "pinned with it."
    )
    assert releases["uv"].target == "ghcr.io/astral-sh/uv:0.14.0"
    assert not releases["litellm"].newer
    assert releases["litellm"].advice() == "The pin is the newest release."
    assert releases["litellm"].scan_image == ""


def test_a_source_build_with_a_newer_release_is_told_to_try_release_mode() -> None:
    kubectl = by_name(upstream_releases.check(PINS, fetcher(DOCUMENTS), COMPONENTS))["kubectl"]

    assert kubectl.mode == "source"
    assert kubectl.advice() == (
        "A newer release exists. Try KUBECTL_MODE=release with KUBECTL_VERSION=v1.37.3, which "
        "installs the upstream binary, and keep it if the image scans clean."
    )


def test_a_built_image_scans_the_newest_upstream_image_in_the_pin_tag_format() -> None:
    trivy = by_name(upstream_releases.check(PINS, fetcher(DOCUMENTS), COMPONENTS))["trivy"]

    assert trivy.scan_image == "docker.io/aquasec/trivy:0.77.0"
    assert trivy.image_pin == "TRIVY_UPSTREAM_IMAGE"
    assert "Try TRIVY_IMAGE_MODE=release if the scan of the upstream image" in trivy.advice()
    assert upstream_releases.scan_targets([trivy]) == [
        {"name": "trivy", "image": "docker.io/aquasec/trivy:0.77.0"}
    ]


def test_an_unreachable_api_is_recorded_and_the_pinned_upstream_image_is_still_scanned() -> None:
    documents = dict(DOCUMENTS)
    documents[github("aquasecurity/trivy", "")[0]] = OSError("Connection refused")

    trivy = by_name(upstream_releases.check(PINS, fetcher(documents), COMPONENTS))["trivy"]

    assert trivy.error == "Connection refused."
    assert trivy.advice() == "The release check failed. Connection refused."
    assert trivy.scan_image == PINS["TRIVY_UPSTREAM_IMAGE"]


def test_the_pinned_upstream_image_is_scanned_when_no_newer_release_exists() -> None:
    documents = dict(DOCUMENTS)
    documents.update([github("aquasecurity/trivy", "v0.76.2")])

    trivy = by_name(upstream_releases.check(PINS, fetcher(documents), COMPONENTS))["trivy"]

    assert trivy.scan_image == PINS["TRIVY_UPSTREAM_IMAGE"]
    assert (
        trivy.advice() == "The pin is the newest release. The platform builds its binary and image."
    )


@pytest.mark.parametrize(
    ("pinned", "newest", "expected"),
    [
        (
            f"ghcr.io/berriai/litellm:v1.109.1@{DIGEST}",
            "v1.110.0",
            "ghcr.io/berriai/litellm:v1.110.0",
        ),
        (f"docker.io/a/b:4.79.0@{DIGEST}", "v4.80.1", "docker.io/a/b:4.80.1"),
        ("docker.io/a/b:1.56.0-python", "v1.57.0", "docker.io/a/b:1.57.0-python"),
        ("docker.io/a/b:latest", "v1.0.0", ""),
    ],
)
def test_a_newer_image_keeps_the_pin_tag_format(pinned: str, newest: str, expected: str) -> None:
    assert upstream_releases.newer_image(pinned, newest) == expected


def test_versions_compare_numerically() -> None:
    assert upstream_releases.is_newer("v26.10.1.5-stable", "26.9.25.15")
    assert not upstream_releases.is_newer("v1.9.0", "v1.10.0")
    assert not upstream_releases.is_newer("nightly", "v1.0.0")


SCAN_OUTPUT = (
    "[image-scan] docker.io/aquasec/trivy:0.77.0 carries 2 blocking finding(s): ...\r\n"
    "[image-scan]   CVE-2026-1 (high) usr/bin/trivy 1.0 fixed in 1.1\r\n"
    "[image-scan]   CVE-2026-1 (high) usr/bin/other 1.0 fixed in 1.1\r\n"
    "[image-scan]   GHSA-xxxx-yyyy (critical) app/package.json 2.0 fixed in no fix\r\n"
)


def write_scans(tmp_path: Path, entries: list[tuple[dict[str, Any], str]]) -> Path:
    lines = []
    for index, (entry, log) in enumerate(entries):
        path = tmp_path / f"{index}.log"
        path.write_text(log, encoding="utf-8")
        lines.append(json.dumps({**entry, "log": str(path)}))
    scans = tmp_path / "scans.jsonl"
    scans.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return scans


def test_scan_records_are_classified_from_the_exit_code_and_the_log(tmp_path: Path) -> None:
    resolved = f"docker.io/aquasec/trivy:0.77.0@{DIGEST}"
    scans = upstream_releases.read_scans(
        write_scans(
            tmp_path,
            [
                ({"image": "clean", "pulled": True, "exit_code": 0, "resolved": resolved}, ""),
                ({"image": "dirty", "pulled": True, "exit_code": 1}, SCAN_OUTPUT),
                ({"image": "absent", "pulled": False, "exit_code": -1}, "manifest unknown"),
                ({"image": "broken", "pulled": True, "exit_code": 1}, "Traceback"),
            ],
        )
    )

    assert {image: scan.status for image, scan in scans.items()} == {
        "clean": upstream_releases.CLEAN,
        "dirty": upstream_releases.FINDINGS,
        "absent": upstream_releases.UNAVAILABLE,
        "broken": upstream_releases.ERROR,
    }
    assert scans["dirty"].findings == ("CVE-2026-1", "GHSA-xxxx-yyyy")
    assert scans["clean"].resolved == resolved
    assert upstream_releases.read_scans(tmp_path / "missing.jsonl") == {}


def test_the_report_names_the_pin_a_clean_upstream_image_allows(tmp_path: Path) -> None:
    documents = dict(DOCUMENTS)
    documents[github("helm/helm", "")[0]] = OSError("HTTP 503")
    pins = {**PINS, "HELM_VERSION": "v4.3.1", "HELM_MODE": "source"}
    releases = upstream_releases.check(pins, fetcher(documents), COMPONENTS)
    resolved = f"docker.io/aquasec/trivy:0.77.0@{DIGEST}"
    scans = {"docker.io/aquasec/trivy:0.77.0": Scan("x", upstream_releases.CLEAN, (), resolved)}

    text = upstream_releases.markdown(releases, scans)

    assert (
        "6 component(s) checked, 4 with a newer release, 1 whose release could not be read." in text
    )
    assert f"Set TRIVY_IMAGE_MODE=release and TRIVY_UPSTREAM_IMAGE={resolved}." in text
    assert "- helm (helm/helm). HTTP 503." in text
    for connector in (" - ", "\u2014", "\u2013", ";"):
        assert connector not in text.replace("| --- ", "")


def test_check_writes_the_result_and_the_scan_targets(tmp_path: Path, monkeypatch) -> None:
    versions = tmp_path / "versions.env"
    versions.write_text(
        "TRIVY_MODE=source\nTRIVY_VERSION=v0.76.2\nTRIVY_IMAGE_MODE=build\n"
        f"TRIVY_UPSTREAM_IMAGE=docker.io/aquasec/trivy:0.76.2@{DIGEST}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(upstream_releases, "fetch_json", fetcher(DOCUMENTS))
    output, targets = tmp_path / "out" / "releases.json", tmp_path / "out" / "targets.json"

    assert (
        upstream_releases.main(
            [
                "check",
                "--versions",
                str(versions),
                "--output",
                str(output),
                "--targets",
                str(targets),
            ]
        )
        == 0
    )

    [trivy] = upstream_releases.read_releases(output)
    assert (trivy.component, trivy.newest) == ("trivy", "v0.77.0")
    assert json.loads(targets.read_text()) == [
        {"name": "trivy", "image": "docker.io/aquasec/trivy:0.77.0"}
    ]


def test_every_component_names_a_repository_and_a_pin_the_versions_file_holds() -> None:
    from scripts import base_versions

    pins = base_versions.load()
    held = [c.name for c in upstream_releases.COMPONENTS if c.pin in pins]
    assert len({c.name for c in upstream_releases.COMPONENTS}) == len(upstream_releases.COMPONENTS)
    assert {"kubectl", "trivy", "semgrep", "litellm", "langfuse-web", "clickhouse"} <= set(held)
    for component in upstream_releases.COMPONENTS:
        assert (
            component.repository == upstream_releases.GO_FEED
            or component.repository.count("/") == 1
        )
