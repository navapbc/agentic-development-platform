"""Fetch and vendor the Atlassian acli binary.

On Linux the binary comes from the versioned Debian package in Atlassian's apt pool and is
verified against a pinned SHA-256 before it is extracted. Atlassian publishes the macOS binary
only as `latest`, so a macOS host fetch is verified only when ACLI_SHA256 is set.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import platform
import stat
import tarfile
from pathlib import Path

import httpx

ACLI_PINNED_VERSION = "1.3.39"
ACLI_PINNED_DEB_SHA256 = {
    "amd64": "92a424beb1cd187521fb0b20165e20c600cb12dbd8d06a2bfdeec50b9e62d19b",
    "arm64": "bfd6f5be1feee2de8f2f5bf817c77bed8876e0f7972dfcff29b4e94875e968f8",
}
_LINUX_DEB_URL_TEMPLATE = (
    "https://acli.atlassian.com/linux/deb/pool/stable/a/ac/acli_{version}-stable_linux_{arch}.deb"
)
_DARWIN_URL_TEMPLATE = "https://acli.atlassian.com/darwin/latest/acli_darwin_{arch}/acli"
_DEB_BINARY_MEMBER = "./usr/bin/acli"
_VENDOR_DEST = "vendor/bin/acli"


def _normalize_os(name: str) -> str:
    name = name.lower()
    if name in {"darwin", "macos"}:
        return "darwin"
    if name == "linux":
        return "linux"
    return name


def _normalize_arch(machine: str) -> str:
    machine = machine.lower()
    if machine in {"x86_64", "amd64"}:
        return "amd64"
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    return machine


def resolve_target() -> tuple[str, str]:
    os_name = _normalize_os(os.environ.get("ACLI_OS") or platform.system())
    arch = _normalize_arch(os.environ.get("ACLI_ARCH") or platform.machine())
    return os_name, arch


def resolve_version() -> str:
    return os.environ.get("ACLI_VERSION", "").strip() or ACLI_PINNED_VERSION


def resolve_url(*, version: str, os_name: str, arch: str, override: str | None) -> str:
    if override:
        return override
    if os_name == "linux":
        return _LINUX_DEB_URL_TEMPLATE.format(version=version, arch=arch)
    if os_name == "darwin":
        return _DARWIN_URL_TEMPLATE.format(arch=arch)
    raise SystemExit(f"acli is not published for {os_name}/{arch}.")


def resolve_expected_sha256(*, version: str, os_name: str, arch: str, override: str | None) -> str:
    """Return the digest the download must match, or empty when none is available.

    An explicit ACLI_SHA256 wins. The pinned digest applies only to the pinned Linux package
    from Atlassian's pool. A Linux download that matches neither is refused.
    """
    explicit = os.environ.get("ACLI_SHA256", "").strip().lower()
    if explicit:
        return explicit
    if os_name == "linux" and not override and version == ACLI_PINNED_VERSION:
        pinned = ACLI_PINNED_DEB_SHA256.get(arch)
        if pinned:
            return pinned
    if os_name == "linux":
        raise SystemExit(
            f"ACLI_SHA256 is required for acli {version} ({arch}) because only "
            f"{ACLI_PINNED_VERSION} carries a pinned digest."
        )
    return ""


def _require_https(request: httpx.Request) -> None:
    if request.url.scheme != "https":
        raise SystemExit(f"acli download refused a non-https URL: {request.url}")


def download(url: str) -> bytes:
    with httpx.Client(
        follow_redirects=True, timeout=120.0, event_hooks={"request": [_require_https]}
    ) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.content


def verify(data: bytes, expected_sha256: str) -> None:
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected_sha256:
        raise SystemExit(f"acli checksum mismatch: expected {expected_sha256}, got {actual}")


def extract_deb_binary(deb: bytes) -> bytes:
    """Return the acli binary from a Debian package without needing dpkg on the host."""
    if not deb.startswith(b"!<arch>\n"):
        raise SystemExit("acli package is not a Debian archive")
    offset = 8
    while offset + 60 <= len(deb):
        header = deb[offset : offset + 60]
        name = header[:16].decode().strip().rstrip("/")
        size = int(header[48:58].decode().strip())
        body = deb[offset + 60 : offset + 60 + size]
        if name.startswith("data.tar"):
            with tarfile.open(fileobj=io.BytesIO(body)) as archive:
                member = archive.extractfile(_DEB_BINARY_MEMBER)
                if member is None:
                    raise SystemExit(f"acli package has no {_DEB_BINARY_MEMBER}")
                return member.read()
        offset += 60 + size + (size % 2)
    raise SystemExit("acli package has no data archive")


def install(data: bytes, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    mode = dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    dest.chmod(mode)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch and vendor the Atlassian acli binary.")
    parser.add_argument("--dest", default=_VENDOR_DEST)
    args = parser.parse_args(argv)

    os_name, arch = resolve_target()
    version = resolve_version()
    override = os.environ.get("ACLI_DOWNLOAD_URL") or None
    url = resolve_url(version=version, os_name=os_name, arch=arch, override=override)
    expected = resolve_expected_sha256(
        version=version, os_name=os_name, arch=arch, override=override
    )
    data = download(url)
    if expected:
        verify(data, expected)
    if url.endswith(".deb"):
        data = extract_deb_binary(data)
    dest = Path(args.dest)
    install(data, dest)
    integrity = "sha256 verified" if expected else "unverified, Atlassian publishes no digest"
    label = version if os_name == "linux" else "latest"
    print(f"acli {label} ({os_name}/{arch}) -> {dest} [{len(data)} bytes, {integrity}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
