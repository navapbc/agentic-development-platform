"""Fetch and vendor the Atlassian acli binary; URL configurable, sha256 optional."""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import stat
import urllib.request
from pathlib import Path

ACLI_VERSION = os.environ.get("ACLI_VERSION", "latest").strip()
_DEFAULT_URL_TEMPLATE = "https://acli.atlassian.com/{os_name}/{version}/acli_{os_name}_{arch}/acli"
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


def resolve_url(*, version: str, os_name: str, arch: str, override: str | None) -> str:
    if override:
        return override
    if not version:
        raise SystemExit("ACLI_VERSION is required to compose the default download URL.")
    return _DEFAULT_URL_TEMPLATE.format(os_name=os_name, arch=arch, version=version)


def resolve_expected_sha256() -> str:
    return os.environ.get("ACLI_SHA256", "").strip().lower()


def download(url: str) -> bytes:
    with urllib.request.urlopen(url) as resp:
        return resp.read()


def verify(data: bytes, expected_sha256: str) -> None:
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected_sha256:
        raise SystemExit(f"acli checksum mismatch: expected {expected_sha256}, got {actual}")


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
    url = resolve_url(
        version=ACLI_VERSION,
        os_name=os_name,
        arch=arch,
        override=os.environ.get("ACLI_DOWNLOAD_URL") or None,
    )
    expected = resolve_expected_sha256()
    data = download(url)
    if expected:
        verify(data, expected)
    dest = Path(args.dest)
    install(data, dest)
    integrity = "sha256 verified" if expected else "unverified (TLS only)"
    print(f"acli ({os_name}/{arch}) -> {dest} [{len(data)} bytes, {integrity}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
