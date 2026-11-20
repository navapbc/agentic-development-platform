from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts import aws_login
from services.aws_credentials import expiry_report, watch_credentials
from tests.support import hermetic_settings

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
HELPER = Path(__file__).resolve().parents[2] / "deploy/kubernetes/local/aws/credential-process.py"


class _Session:
    def __init__(self, token: str = "", expiry: datetime | None = None) -> None:
        self._token = token
        self._expiry = expiry

    def get_credentials(self):
        credentials = SimpleNamespace(
            get_frozen_credentials=lambda: SimpleNamespace(
                access_key="AKIA", secret_key="secret", token=self._token
            )
        )
        if self._expiry is not None:
            credentials._expiry_time = self._expiry
        return credentials


class _Sts:
    def __init__(self) -> None:
        self.durations: list[int] = []

    def get_session_token(self, DurationSeconds: int):  # noqa: N803, boto3 names the parameter
        self.durations.append(DurationSeconds)
        return {
            "Credentials": {
                "AccessKeyId": "ASIA",
                "SecretAccessKey": "s",
                "SessionToken": "t",
                "Expiration": NOW + timedelta(seconds=DurationSeconds),
            }
        }


def test_a_static_key_is_exchanged_for_session_credentials_of_the_configured_length() -> None:
    sts = _Sts()

    record = aws_login.obtain(
        hermetic_settings(aws_login_session_seconds=3600),
        environ={},
        session_factory=lambda profile: _Session(),
        client_factory=lambda record, region: sts,
    )

    assert sts.durations == [3600]
    assert record == {
        "Version": 1,
        "AccessKeyId": "ASIA",
        "SecretAccessKey": "s",
        "SessionToken": "t",
        "Expiration": "2026-11-07T13:00:00Z",
    }


def test_a_static_key_passes_through_when_the_setting_allows() -> None:
    record = aws_login.obtain(
        hermetic_settings(aws_login_static_passthrough=True),
        environ={},
        session_factory=lambda profile: _Session(),
        client_factory=lambda record, region: pytest.fail("no session token is minted"),
    )

    assert record == {"Version": 1, "AccessKeyId": "AKIA", "SecretAccessKey": "secret"}


def test_session_credentials_keep_their_token_and_expiry() -> None:
    record = aws_login.obtain(
        hermetic_settings(),
        environ={},
        session_factory=lambda profile: _Session(token="t", expiry=NOW),
    )

    assert record["SessionToken"] == "t"
    assert record["Expiration"] == "2026-11-07T12:00:00Z"


def test_the_platform_profile_takes_precedence_over_the_shell_profile() -> None:
    seen: list[str] = []

    aws_login.obtain(
        hermetic_settings(aws_profile="shell", aws_login_static_passthrough=True),
        environ={"PLATFORM_AWS_PROFILE": "platform"},
        session_factory=lambda profile: seen.append(profile) or _Session(),
    )

    assert seen == ["platform"]


def test_a_login_command_supplies_the_credentials() -> None:
    printed = json.dumps(
        {
            "Version": 1,
            "AccessKeyId": "ASIA",
            "SecretAccessKey": "s",
            "SessionToken": "t",
            "Expiration": "2026-11-07T14:00:00Z",
        }
    )
    calls: list[list[str]] = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, printed, "")

    record = aws_login.obtain(
        hermetic_settings(aws_login_command="kion favorite dev --credential-process"),
        environ={},
        command_runner=runner,
    )

    assert calls == [["kion", "favorite", "dev", "--credential-process"]]
    assert record["Expiration"] == "2026-11-07T14:00:00Z"


def test_a_login_command_that_prints_no_credentials_is_refused() -> None:
    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, "signed in", "")

    with pytest.raises(aws_login.LoginError, match="credential_process JSON"):
        aws_login.obtain(
            hermetic_settings(aws_login_command="login"), environ={}, command_runner=runner
        )


def test_the_secret_carries_the_record_in_the_configured_namespace() -> None:
    manifest = aws_login.secret_manifest(hermetic_settings(k8s_namespace="ns"), {"Version": 1})

    assert manifest["metadata"]["namespace"] == "ns"
    assert manifest["metadata"]["name"] == aws_login.CREDENTIALS_OBJECT
    assert set(manifest["data"]) == {"credentials.json"}


@pytest.mark.parametrize(
    ("expiration", "renew", "words"),
    [
        (None, False, "carry no expiry"),
        ("2026-11-07T13:00:00Z", False, "expire at 2026-11-07T13:00:00Z"),
        ("2026-11-07T12:10:00Z", True, "in 10 minutes. Run inv aws-login"),
        ("2026-11-07T11:00:00Z", True, "expired at 2026-11-07T11:00:00Z. Run inv aws-login"),
    ],
)
def test_the_expiry_report_names_the_renewal_command_when_it_is_due(
    expiration, renew, words
) -> None:
    record = {"Expiration": expiration} if expiration else {}

    due, report = expiry_report(record, 900, NOW)

    assert due is renew
    assert words in report


def test_the_worker_warns_while_the_credentials_are_near_expiry(tmp_path: Path, caplog) -> None:
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps({"Expiration": "2026-11-07T12:05:00Z"}))

    async def one_pass() -> None:
        task = asyncio.create_task(
            watch_credentials(path, warn_seconds=900, interval=3600, now=lambda: NOW)
        )
        await asyncio.sleep(0)
        task.cancel()

    with caplog.at_level(logging.WARNING):
        asyncio.run(one_pass())

    assert "Run inv aws-login" in caplog.text


def _run_helper(tmp_path: Path, record: dict) -> dict:
    stored = tmp_path / "credentials.json"
    stored.write_text(json.dumps(record))
    source = HELPER.read_text().replace("/var/run/aws-credentials/credentials.json", str(stored))
    result = subprocess.run(
        [sys.executable, "-c", source], capture_output=True, text=True, check=True
    )
    return json.loads(result.stdout)


def test_the_pod_helper_caps_the_expiry_so_the_sdk_rereads_the_secret(tmp_path: Path) -> None:
    far = (datetime.now(UTC) + timedelta(hours=12)).isoformat().replace("+00:00", "Z")

    printed = _run_helper(tmp_path, {"Version": 1, "AccessKeyId": "A", "Expiration": far})
    expiry = datetime.fromisoformat(printed["Expiration"].replace("Z", "+00:00"))

    assert expiry <= datetime.now(UTC) + timedelta(minutes=21)


def test_the_pod_helper_gives_credentials_without_an_expiry_one(tmp_path: Path) -> None:
    printed = _run_helper(tmp_path, {"Version": 1, "AccessKeyId": "A"})

    assert printed["Expiration"].endswith("Z")
