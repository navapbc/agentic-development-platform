import hashlib

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


def test_resolve_url_default_composes_atlassian_url():
    url = fetch_acli.resolve_url(version="1.2.3", os_name="linux", arch="amd64", override=None)
    assert "linux" in url
    assert "amd64" in url
    assert "1.2.3" in url


def test_resolve_url_default_requires_version():
    with pytest.raises(SystemExit):
        fetch_acli.resolve_url(version="", os_name="linux", arch="amd64", override=None)


def test_normalize_arch_maps_common_aliases():
    assert fetch_acli._normalize_arch("x86_64") == "amd64"
    assert fetch_acli._normalize_arch("aarch64") == "arm64"


def test_verify_accepts_matching_digest():
    data = b"binary-bytes"
    fetch_acli.verify(data, hashlib.sha256(data).hexdigest())


def test_verify_rejects_mismatch():
    with pytest.raises(SystemExit):
        fetch_acli.verify(b"binary-bytes", "0" * 64)


def test_install_writes_executable_binary(tmp_path):
    dest = tmp_path / "bin" / "acli"
    fetch_acli.install(b"payload", dest)
    assert dest.read_bytes() == b"payload"
    assert dest.stat().st_mode & 0o111
