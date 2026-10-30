import hashlib
import io
import tarfile

import httpx
import pytest
from scripts import fetch_acli


def test_resolve_url_uses_override_verbatim():
    url = fetch_acli.resolve_url(
        version="1.2.3",
        os_name="linux",
        arch="amd64",
        override="https://artifactory.internal/project/jira/acli/acli",
    )
    assert url == "https://artifactory.internal/project/jira/acli/acli"


def test_resolve_url_composes_the_versioned_linux_package_url():
    url = fetch_acli.resolve_url(version="1.3.39", os_name="linux", arch="arm64", override=None)
    assert url == (
        "https://acli.atlassian.com/linux/deb/pool/stable/a/ac/acli_1.3.39-stable_linux_arm64.deb"
    )


def test_resolve_url_uses_the_latest_macos_binary():
    url = fetch_acli.resolve_url(version="1.3.39", os_name="darwin", arch="arm64", override=None)
    assert url == "https://acli.atlassian.com/darwin/latest/acli_darwin_arm64/acli"


def test_resolve_version_defaults_to_the_pinned_release(monkeypatch):
    monkeypatch.delenv("ACLI_VERSION", raising=False)
    assert fetch_acli.resolve_version() == fetch_acli.ACLI_PINNED_VERSION


def test_normalize_arch_maps_common_aliases():
    assert fetch_acli._normalize_arch("x86_64") == "amd64"
    assert fetch_acli._normalize_arch("aarch64") == "arm64"


def test_verify_accepts_matching_digest():
    data = b"binary-bytes"
    fetch_acli.verify(data, hashlib.sha256(data).hexdigest())


def test_verify_rejects_mismatch():
    with pytest.raises(SystemExit):
        fetch_acli.verify(b"binary-bytes", "0" * 64)


def test_pinned_linux_package_uses_the_committed_digest(monkeypatch):
    monkeypatch.delenv("ACLI_SHA256", raising=False)
    for arch, digest in fetch_acli.ACLI_PINNED_DEB_SHA256.items():
        expected = fetch_acli.resolve_expected_sha256(
            version=fetch_acli.ACLI_PINNED_VERSION, os_name="linux", arch=arch, override=None
        )
        assert expected == digest


def test_explicit_digest_overrides_the_pinned_one(monkeypatch):
    monkeypatch.setenv("ACLI_SHA256", "ABC123")
    expected = fetch_acli.resolve_expected_sha256(
        version=fetch_acli.ACLI_PINNED_VERSION, os_name="linux", arch="amd64", override=None
    )
    assert expected == "abc123"


@pytest.mark.parametrize(
    ("version", "override"),
    [("1.3.40", None), (fetch_acli.ACLI_PINNED_VERSION, "https://mirror.internal/acli.deb")],
)
def test_linux_download_without_a_known_digest_is_refused(monkeypatch, version, override):
    monkeypatch.delenv("ACLI_SHA256", raising=False)
    with pytest.raises(SystemExit, match="ACLI_SHA256 is required"):
        fetch_acli.resolve_expected_sha256(
            version=version, os_name="linux", arch="amd64", override=override
        )


def test_macos_download_digest_is_optional(monkeypatch):
    monkeypatch.delenv("ACLI_SHA256", raising=False)
    expected = fetch_acli.resolve_expected_sha256(
        version=fetch_acli.ACLI_PINNED_VERSION, os_name="darwin", arch="arm64", override=None
    )
    assert expected == ""


def _deb(payload: bytes) -> bytes:
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as archive:
        info = tarfile.TarInfo("./usr/bin/acli")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    members = [(b"debian-binary", b"2.0\n"), (b"data.tar.gz", data.getvalue())]
    out = b"!<arch>\n"
    for name, body in members:
        out += name.ljust(16) + b"0".ljust(12) + b"0".ljust(6) + b"0".ljust(6)
        out += b"100644".ljust(8) + str(len(body)).encode().ljust(10) + b"`\n" + body
        out += b"\n" if len(body) % 2 else b""
    return out


def test_extract_deb_binary_returns_the_packaged_acli():
    assert fetch_acli.extract_deb_binary(_deb(b"acli-binary")) == b"acli-binary"


def test_extract_deb_binary_rejects_a_non_debian_file():
    with pytest.raises(SystemExit, match="not a Debian archive"):
        fetch_acli.extract_deb_binary(b"plain-binary")


def test_install_writes_executable_binary(tmp_path):
    dest = tmp_path / "bin" / "acli"
    fetch_acli.install(b"payload", dest)
    assert dest.read_bytes() == b"payload"
    assert dest.stat().st_mode & 0o111


def test_download_refuses_a_non_https_url():
    with pytest.raises(SystemExit, match="non-https"):
        fetch_acli.download("http://acli.example.test/acli")


def test_download_refuses_a_redirect_to_http(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://mirror.example.test/acli"})

    real_client = httpx.Client

    def client_with_mock_transport(**kwargs):
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(fetch_acli.httpx, "Client", client_with_mock_transport)
    with pytest.raises(SystemExit, match="non-https"):
        fetch_acli.download("https://acli.example.test/acli")
