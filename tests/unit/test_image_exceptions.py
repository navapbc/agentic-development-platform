"""The two levels of image exceptions are parsed, scoped, read, and written consistently."""

from __future__ import annotations

import io
import json
import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from botocore.exceptions import ClientError
from scripts import image_exceptions as cli
from services.integration import image_exceptions
from services.integration.credentials import ConfigError
from services.integration.image_exceptions import (
    PLATFORM_KEY,
    S3File,
    exceptions_setup_problems,
    gate_exceptions,
    platform_exceptions,
    read_platform,
    update,
)
from services.integration.image_policy import (
    PLATFORM_SCOPE,
    TENANT_SCOPE,
    FindingException,
    in_platform_scope,
    in_tenant_scope,
    parse_exceptions,
    parse_policy,
    require_scope,
)
from tests.support import hermetic_settings

HOST = "111111111111.dkr.ecr.us-east-1.amazonaws.com"
DIGEST = "sha256:" + "a" * 64
POLICY_YAML = f"""config_version: 1
platform_registries: [{HOST}]
catalog: [docker.io/aquasec/trivy]
tenant_prefix: {HOST}/agentic-platform/tenants/{{tenant}}/
"""
POLICY = parse_policy(yaml.safe_load(POLICY_YAML))
PLATFORM_IMAGE = f"{HOST}/agentic-platform/temporal-worker@{DIGEST}"
TENANT_IMAGE = f"{HOST}/agentic-platform/tenants/adp/app@{DIGEST}"


def _today() -> date:
    return datetime.now(UTC).date()


def entry(image: str = PLATFORM_IMAGE, finding: str = "CVE-1", **overrides: Any) -> dict:
    today = _today()
    values = {
        "image": image,
        "finding": finding,
        "reason": "Vendor fix pending.",
        "created": today.isoformat(),
        "expires": (today + timedelta(days=30)).isoformat(),
        "requester": "alice",
        "approver": "bob",
    }
    values.update(overrides)
    return {key: value for key, value in values.items() if value is not None}


def document(*entries: dict) -> dict:
    return {"config_version": 1, "exceptions": list(entries)}


def write(path: Path, *entries: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document(*entries), sort_keys=False), encoding="utf-8")


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(image_exceptions, "_texts", {})


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "env"
    directory.mkdir()
    (directory / "image_policy.yaml").write_text(POLICY_YAML, encoding="utf-8")
    monkeypatch.setenv("PLATFORM_ENV_CONFIG_DIR", str(directory))
    return directory


def settings(**overrides: Any) -> Any:
    values: dict[str, Any] = {"environment": "test", "tenant": "adp", "tenant_config_bucket": ""}
    values.update(overrides)
    return hermetic_settings(**values)


@pytest.mark.parametrize(
    ("raw", "scope", "reason"),
    [
        ({"exceptions": []}, PLATFORM_SCOPE, "config_version must be 1"),
        (document(entry(reason=" ")), PLATFORM_SCOPE, "needs reason"),
        (document(entry(created=None)), TENANT_SCOPE, "needs created"),
        (document(entry(approver=None)), PLATFORM_SCOPE, "needs approver"),
        (document(entry(requester=None)), PLATFORM_SCOPE, "needs requester"),
        (
            document(entry(expires=(_today() + timedelta(days=31)).isoformat())),
            TENANT_SCOPE,
            "more than 30 days after it was created",
        ),
        (document(entry(), entry()), PLATFORM_SCOPE, r"exceptions\[1\] accepts CVE-1 .* second"),
    ],
)
def test_an_entry_without_a_required_field_or_beyond_the_window_is_refused(
    raw, scope, reason
) -> None:
    with pytest.raises(ConfigError, match=reason):
        parse_exceptions(raw, scope=scope)


def test_a_tenant_entry_needs_no_requester_or_approver() -> None:
    [parsed] = parse_exceptions(
        document(entry(TENANT_IMAGE, requester=None, approver=None)), scope=TENANT_SCOPE
    )

    assert parsed.requester == parsed.approver == ""
    assert parsed.created == _today()


def test_the_platform_scope_holds_the_catalog_and_its_own_repositories() -> None:
    assert in_platform_scope("docker.io/aquasec/trivy", POLICY)
    assert in_platform_scope(f"{HOST}/agentic-platform/temporal-worker", POLICY)
    assert not in_platform_scope(f"{HOST}/agentic-platform/tenants/adp/app", POLICY)
    assert not in_platform_scope("docker.io/library/python", POLICY)


def test_a_tenant_scope_holds_its_prefix_and_its_external_mirrors() -> None:
    external = {"ghcr.io/acme": "artifactory.example.gov/ghcr-remote/acme"}
    into_platform = {"docker.io/library": f"{HOST}/agentic-platform-ecr-public/docker/library"}

    assert in_tenant_scope(f"{HOST}/agentic-platform/tenants/adp/app", POLICY, "adp")
    assert not in_tenant_scope(f"{HOST}/agentic-platform/tenants/other/app", POLICY, "adp")
    assert not in_tenant_scope(f"{HOST}/agentic-platform/temporal-worker", POLICY, "adp")
    assert in_tenant_scope("ghcr.io/acme/tool", POLICY, "adp", external)
    assert in_tenant_scope("artifactory.example.gov/ghcr-remote/acme/tool", POLICY, "adp", external)
    assert not in_tenant_scope("docker.io/library/python", POLICY, "adp", into_platform)


def test_an_entry_outside_its_owners_scope_is_refused_by_name() -> None:
    [tenant_entry] = parse_exceptions(document(entry(TENANT_IMAGE)))
    [platform_entry] = parse_exceptions(document(entry()))

    with pytest.raises(ConfigError, match=r"x.yaml exceptions\[0\] accepts CVE-1 in .*tenants/adp"):
        require_scope([tenant_entry], PLATFORM_SCOPE, POLICY, source="x.yaml")
    with pytest.raises(ConfigError, match="outside the scope of tenant adp"):
        require_scope([platform_entry], TENANT_SCOPE, POLICY, tenant="adp")


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "Operation")


class FakeS3:
    """A versioned bucket that honours IfMatch and IfNoneMatch on PutObject."""

    def __init__(self) -> None:
        self.objects: dict[str, list[tuple[str, str]]] = {}
        self.gets = 0
        self.before_put: Any = None

    def _latest(self, key: str) -> tuple[str, str]:
        if key not in self.objects:
            raise _client_error("404")
        return self.objects[key][-1]

    def store(self, key: str, text: str) -> str:
        versions = self.objects.setdefault(key, [])
        version = f"v{len(versions) + 1}"
        versions.append((version, text))
        return version

    def head_object(self, Bucket: str, Key: str) -> dict:  # noqa: N803
        version, _ = self._latest(Key)
        return {"VersionId": version, "ETag": f'"{version}"'}

    def get_object(self, Bucket: str, Key: str, VersionId: str = "") -> dict:  # noqa: N803
        self.gets += 1
        text = dict(self.objects[Key])[VersionId]
        return {"Body": io.BytesIO(text.encode()), "ETag": f'"{VersionId}"'}

    def put_object(self, Bucket: str, Key: str, Body: bytes, ContentType: str, **condition):  # noqa: N803
        if self.before_put is not None:
            hook, self.before_put = self.before_put, None
            hook()
        current = self.objects.get(Key)
        if "IfNoneMatch" in condition and current:
            raise _client_error("PreconditionFailed")
        if "IfMatch" in condition and condition["IfMatch"] != f'"{current[-1][0]}"':
            raise _client_error("PreconditionFailed")
        return {"VersionId": self.store(Key, Body.decode())}


def test_a_conditional_write_retries_and_keeps_a_concurrent_writers_entry() -> None:
    s3 = FakeS3()
    s3.store(PLATFORM_KEY, yaml.safe_dump(document(entry(finding="CVE-1"))))
    file = S3File(s3, "config", PLATFORM_KEY)
    s3.before_put = lambda: s3.store(
        PLATFORM_KEY, yaml.safe_dump(document(entry(finding="CVE-1"), entry(finding="CVE-2")))
    )
    [added] = parse_exceptions(document(entry(finding="CVE-3")))

    result = update(file, lambda current: (*current, added), scope=PLATFORM_SCOPE)

    assert result.written
    assert result.version == "v3"
    assert [e.finding for e in result.after] == ["CVE-1", "CVE-2", "CVE-3"]


def test_an_s3_file_is_read_once_per_version() -> None:
    s3 = FakeS3()
    s3.store("platform/x.yaml", "config_version: 1\nexceptions: []\n")
    file = S3File(s3, "cache-test", "platform/x.yaml")

    assert file.read().version == file.read().version == "v1"
    assert s3.gets == 1
    assert not S3File(s3, "cache-test", "platform/absent.yaml").read().exists


def test_the_platform_file_comes_from_the_bucket_when_one_is_configured(env: Path) -> None:
    s3 = FakeS3()
    s3.store(PLATFORM_KEY, yaml.safe_dump(document(entry())))

    snapshot, [parsed] = read_platform(settings(tenant_config_bucket="config"), client=s3)

    assert snapshot.location == f"s3://config/{PLATFORM_KEY}"
    assert parsed.approver == "bob"


def test_the_setup_check_names_the_path_or_key_it_looked_for(env: Path) -> None:
    [local] = exceptions_setup_problems(settings())
    [remote] = exceptions_setup_problems(settings(tenant_config_bucket="config"), FakeS3())

    assert str(env / "image_exceptions.yaml") in local
    assert f"s3://config/{PLATFORM_KEY} is missing" in remote
    assert "tenant-config-store" in remote
    write(env / "image_exceptions.yaml")
    assert exceptions_setup_problems(settings()) == []


def test_the_scan_reads_no_exception_from_a_missing_file_and_refuses_an_invalid_one(
    env: Path, tmp_path: Path
) -> None:
    assert platform_exceptions(settings()) == ()
    override = tmp_path / "override.yaml"
    write(override, entry())
    assert [e.finding for e in platform_exceptions(settings(), str(override))] == ["CVE-1"]
    write(override, entry(TENANT_IMAGE))
    with pytest.raises(ConfigError, match="outside the platform's scope"):
        platform_exceptions(settings(), str(override))


def test_the_gate_honours_each_file_within_its_scope_and_ignores_an_unusable_one(
    env: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import scripts.config

    monkeypatch.setattr(scripts.config, "get_settings", settings)
    tenant_file = env / "tenants" / "adp" / "image_exceptions.yaml"
    write(tenant_file, entry(TENANT_IMAGE, finding="CVE-2"))

    with caplog.at_level(logging.WARNING):
        assert [e.finding for e in gate_exceptions("test")] == ["CVE-2"]
    assert "is missing, so the pull gate accepts no platform finding" in caplog.text

    write(env / "image_exceptions.yaml", entry())
    write(tenant_file, entry(PLATFORM_IMAGE, finding="CVE-2"))
    assert [e.finding for e in gate_exceptions("test")] == ["CVE-1"]


def run_cli(*argv: str) -> int:
    return cli.main(list(argv))


def add_args(*extra: str, finding: str = "CVE-1", image: str = PLATFORM_IMAGE) -> list[str]:
    expires = (_today() + timedelta(days=10)).isoformat()
    return [
        "add",
        "--local",
        "--image",
        image,
        "--finding",
        finding,
        "--reason",
        "Vendor fix pending.",
        "--expires",
        expires,
        *extra,
    ]


@pytest.fixture
def local(env: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(cli, "_settings", lambda tenant="": settings(tenant=tenant or "adp"))
    write(env / "image_exceptions.yaml")
    (env / "image_exceptions.yaml").write_text(
        "# Platform exceptions.\n" + (env / "image_exceptions.yaml").read_text(), encoding="utf-8"
    )
    return env


def test_add_writes_a_platform_entry_with_its_requester_approver_and_creation_day(
    local: Path,
) -> None:
    assert run_cli(*add_args("--requester", "alice", "--approver", "bob")) == 0
    assert run_cli(*add_args("--requester", "alice", "--approver", "carol")) == 0

    text = (local / "image_exceptions.yaml").read_text()
    [written] = yaml.safe_load(text)["exceptions"]
    assert text.startswith("# Platform exceptions.\n")
    assert written["approver"] == "carol"
    assert written["created"] == _today()


def test_add_writes_a_tenant_entry_to_the_tenant_file(local: Path) -> None:
    assert run_cli(*add_args("--scope", "tenant", image=TENANT_IMAGE)) == 0

    tenant = yaml.safe_load((local / "tenants" / "adp" / "image_exceptions.yaml").read_text())
    assert tenant["exceptions"][0]["image"] == TENANT_IMAGE


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (add_args("--approver", "bob", image=TENANT_IMAGE), "outside the platform's scope"),
        (add_args("--scope", "tenant"), "outside the scope of tenant adp"),
        (add_args(), "needs approver"),
    ],
)
def test_add_refuses_an_invalid_entry(
    local: Path, capsys: pytest.CaptureFixture[str], argv: list[str], message: str
) -> None:
    assert run_cli(*argv) == 1
    assert message in capsys.readouterr().err


def test_add_refuses_an_expiry_beyond_the_window(local: Path, capsys) -> None:
    argv = add_args("--approver", "bob")
    argv[argv.index("--expires") + 1] = (_today() + timedelta(days=31)).isoformat()

    assert run_cli(*argv) == 1
    assert "more than 30 days" in capsys.readouterr().err


def test_add_refuses_to_run_locally_when_a_bucket_is_configured(
    env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "_settings", lambda tenant="": settings(tenant_config_bucket="b"))

    assert run_cli(*add_args("--approver", "bob")) == 2
    assert run_cli("prune", "--local") == 2
    assert "TENANT_CONFIG_BUCKET is set" in capsys.readouterr().err


def test_add_reads_the_approver_from_the_run_approvals_and_requires_an_active_finding(
    local: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    approvals = tmp_path / "approvals.json"
    approvals.write_text(
        json.dumps(
            [
                {"state": "approved", "environments": [{"name": "other"}], "user": {"login": "x"}},
                {
                    "state": "approved",
                    "environments": [{"name": "image-exception"}],
                    "user": {"login": "reviewer"},
                },
            ]
        ),
        encoding="utf-8",
    )
    scan_log = tmp_path / "scan.log"
    scan_log.write_text("[image-scan]   CVE-1 (high) /usr/lib/x 1 fixed in 2\n", encoding="utf-8")
    base = ("--approvals", str(approvals), "--scan-log", str(scan_log))

    assert run_cli(*add_args(*base, finding="CVE-9")) == 1
    assert "does not report CVE-9" in capsys.readouterr().err
    assert run_cli(*add_args(*base, "--dry-run")) == 0
    assert yaml.safe_load((local / "image_exceptions.yaml").read_text())["exceptions"] == []
    assert run_cli(*add_args(*base)) == 0
    [written] = yaml.safe_load((local / "image_exceptions.yaml").read_text())["exceptions"]
    assert written["approver"] == "reviewer"


def test_prune_removes_only_the_expired_entries(local: Path) -> None:
    today = _today()
    expired = entry(
        finding="CVE-1",
        created=(today - timedelta(days=20)).isoformat(),
        expires=(today - timedelta(days=1)).isoformat(),
    )
    write(local / "image_exceptions.yaml", expired, entry(finding="CVE-2"))

    assert run_cli("prune", "--local") == 0

    remaining = yaml.safe_load((local / "image_exceptions.yaml").read_text())["exceptions"]
    assert [e["finding"] for e in remaining] == ["CVE-2"]


def test_the_scanner_shares_the_parser(tmp_path: Path) -> None:
    from scripts.image_scan import load_exceptions

    path = tmp_path / "image_exceptions.yaml"
    write(path, entry())

    assert load_exceptions(str(path)) == (
        FindingException(
            PLATFORM_IMAGE,
            "CVE-1",
            "Vendor fix pending.",
            _today() + timedelta(days=30),
            _today(),
            "alice",
            "bob",
        ),
    )
    assert load_exceptions("") == ()
    path.write_text("exceptions: []\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="config_version must be 1"):
        load_exceptions(str(path))
