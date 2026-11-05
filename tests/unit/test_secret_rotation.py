"""Tests for the secret-store rotation and role bootstrap function."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from botocore.exceptions import ClientError

_SOURCE = Path(__file__).resolve().parents[2] / "infra/terraform/secret-store/functions/rotation.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("secret_store_rotation", _SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rotation = _load()

ARN = "arn:aws:secretsmanager:us-east-1:111111111111:secret:platform/db/temporal-abc"
USER_ARN = "arn:aws:elasticache:us-east-1:111111111111:user:platform-langfuse"


class _NotFound(Exception):
    pass


class FakeSecrets:
    class exceptions:
        ResourceNotFoundException = _NotFound

    def __init__(self, current: dict[str, Any], *, rotation_enabled: bool = True) -> None:
        self.rotation_enabled = rotation_enabled
        self.versions: dict[str, tuple[set[str], dict[str, Any]]] = {
            "v1": ({rotation.AWSCURRENT}, current)
        }
        self.puts: list[dict[str, Any]] = []

    def stage(self, token: str, value: dict[str, Any] | None = None) -> None:
        self.versions[token] = ({rotation.AWSPENDING}, value or {})

    def describe_secret(self, SecretId: str) -> dict[str, Any]:
        return {
            "RotationEnabled": self.rotation_enabled,
            "VersionIdsToStages": {v: sorted(s) for v, (s, _) in self.versions.items()},
        }

    def get_secret_value(
        self,
        SecretId: str,
        VersionStage: str,
        VersionId: str | None = None,
    ) -> dict[str, str]:
        for version, (stages, value) in self.versions.items():
            if VersionStage in stages and VersionId in (None, version) and value:
                return {"SecretString": json.dumps(value)}
        raise _NotFound

    def get_random_password(self, **_: Any) -> dict[str, str]:
        return {"RandomPassword": "n" * rotation.PASSWORD_LENGTH}

    def put_secret_value(self, **kwargs: Any) -> None:
        self.puts.append(kwargs)
        stages = set(kwargs["VersionStages"])
        self.versions[kwargs["ClientRequestToken"]] = (stages, json.loads(kwargs["SecretString"]))

    def update_secret_version_stage(self, **kwargs: Any) -> None:
        self.versions[kwargs["RemoveFromVersionId"]][0].discard(rotation.AWSCURRENT)
        self.versions[kwargs["MoveToVersionId"]][0].add(rotation.AWSCURRENT)


def _event(step: str, token: str = "v2") -> dict[str, str]:
    return {"SecretId": ARN, "ClientRequestToken": token, "Step": step}


def _noop_rotate(event: dict[str, str], client: FakeSecrets) -> list[str]:
    calls: list[str] = []
    rotation.rotate(
        event,
        client,
        set_secret=lambda _c, _p: calls.append("set"),
        test_secret=lambda _p: calls.append("test"),
    )
    return calls


def test_create_secret_stages_a_new_password_and_keeps_the_other_fields():
    client = FakeSecrets({"username": "temporal", "password": "old"})
    client.stage("v2")

    _noop_rotate(_event("createSecret"), client)

    pending = json.loads(client.puts[0]["SecretString"])
    assert pending == {"username": "temporal", "password": "n" * rotation.PASSWORD_LENGTH}
    assert client.puts[0]["VersionStages"] == [rotation.AWSPENDING]


def test_create_secret_is_idempotent_when_the_pending_version_exists():
    client = FakeSecrets({"username": "temporal", "password": "old"})
    client.stage("v2", {"username": "temporal", "password": "staged"})

    _noop_rotate(_event("createSecret"), client)

    assert client.puts == []


def test_set_and_test_steps_receive_the_pending_credential():
    client = FakeSecrets({"username": "temporal", "password": "old"})
    client.stage("v2", {"username": "temporal", "password": "new"})
    seen: list[tuple[str, str]] = []

    for step in ("setSecret", "testSecret"):
        rotation.rotate(
            _event(step),
            client,
            set_secret=lambda c, p: seen.append((c["password"], p["password"])),
            test_secret=lambda p: seen.append(("test", p["password"])),
        )

    assert seen == [("old", "new"), ("test", "new")]


def test_finish_secret_moves_awscurrent_to_the_pending_version():
    client = FakeSecrets({"username": "temporal", "password": "old"})
    client.stage("v2", {"username": "temporal", "password": "new"})

    _noop_rotate(_event("finishSecret"), client)

    assert rotation.AWSCURRENT in client.versions["v2"][0]
    assert rotation.AWSCURRENT not in client.versions["v1"][0]


def test_a_version_that_is_already_current_is_left_alone():
    client = FakeSecrets({"username": "temporal", "password": "old"})

    assert _noop_rotate(_event("setSecret", token="v1"), client) == []


@pytest.mark.parametrize(
    ("client", "token"),
    [
        (FakeSecrets({"password": "old"}, rotation_enabled=False), "v1"),
        (FakeSecrets({"password": "old"}), "unknown"),
    ],
)
def test_rotation_refuses_a_secret_it_cannot_act_on(client, token):
    with pytest.raises(rotation.RotationError):
        _noop_rotate(_event("createSecret", token=token), client)


def test_scram_verifier_has_the_postgres_format_and_hides_the_password():
    verifier = rotation.scram_sha256_verifier("pencil", salt=b"0123456789abcdef")

    assert re.fullmatch(
        r"SCRAM-SHA-256\$4096:[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+:[A-Za-z0-9+/=]+", verifier
    )
    assert verifier == rotation.scram_sha256_verifier("pencil", salt=b"0123456789abcdef")
    assert "pencil" not in verifier


def test_password_statement_carries_a_verifier_and_never_the_password():
    statement = rotation.password_statement("temporal", "s3cret-value")

    assert statement.startswith('ALTER ROLE "temporal" WITH LOGIN PASSWORD \'SCRAM-SHA-256$')
    assert "s3cret-value" not in statement


@pytest.mark.parametrize("name", ["Temporal", 'temporal"; DROP ROLE x; --', "1role", ""])
def test_identifiers_outside_the_lowercase_form_are_rejected(name):
    with pytest.raises(ValueError):
        rotation.quote_identifier(name)


def _credentials() -> dict[str, dict[str, str]]:
    return {key: {"username": key, "password": f"{key}-pw"} for key in rotation.ROLE_KEYS}


def _plan(existing: frozenset[str] = frozenset(), roles: frozenset[str] = frozenset()) -> list[Any]:
    return rotation.bootstrap_plan(
        _credentials(),
        existing_roles=roles,
        existing_databases=existing,
        platform_database="agent_platform",
        platform_schema="agent_platform",
    )


def test_bootstrap_creates_absent_service_databases_and_reowns_existing_ones():
    statements = [sql for step in _plan(frozenset({"langfuse"})) for sql in step.statements]

    assert 'CREATE DATABASE "temporal" OWNER "temporal"' in statements
    assert 'CREATE DATABASE "temporal_visibility" OWNER "temporal"' in statements
    assert 'ALTER DATABASE "langfuse" OWNER TO "langfuse"' in statements
    for database in ("temporal", "temporal_visibility", "langfuse", "agent_platform"):
        assert f'REVOKE ALL ON DATABASE "{database}" FROM PUBLIC' in statements


def test_bootstrap_creates_only_the_absent_roles_and_resets_every_password():
    role_step = _plan(roles=frozenset({"temporal"}))[0]

    assert 'CREATE ROLE "temporal" LOGIN' not in role_step.statements
    assert 'CREATE ROLE "langfuse" LOGIN' in role_step.statements
    for key in rotation.ROLE_KEYS:
        assert any(sql.startswith(f'ALTER ROLE "{key}" WITH LOGIN') for sql in role_step.statements)
        assert f'GRANT "{key}" TO CURRENT_USER' in role_step.statements


def test_bootstrap_installs_pgaudit_in_every_service_database():
    audited = {
        step.database
        for step in _plan()
        if step.statements == ("CREATE EXTENSION IF NOT EXISTS pgaudit",)
    }

    assert audited == {"agent_platform", "temporal", "temporal_visibility", "langfuse"}


def test_bootstrap_runs_the_platform_schema_grants_as_the_migrator_in_one_transaction():
    schema_step = _plan()[-1]

    assert schema_step.transactional
    assert schema_step.database == "agent_platform"
    assert schema_step.statements[0] == 'SET LOCAL ROLE "platform_migrator"'
    assert schema_step.statements[-1] == (
        'REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON "agent_platform".alembic_version '
        'FROM "platform_app"'
    )


def test_bootstrap_never_sends_a_plaintext_password():
    statements = [sql for step in _plan() for sql in step.statements]

    for key in rotation.ROLE_KEYS:
        assert not any(f"{key}-pw" in sql for sql in statements)


def test_bootstrap_requires_every_service_credential():
    credentials = _credentials()
    del credentials[rotation.LANGFUSE]

    with pytest.raises(rotation.RotationError):
        rotation.bootstrap_plan(
            credentials,
            existing_roles=frozenset(),
            existing_databases=frozenset(),
            platform_database="agent_platform",
            platform_schema="agent_platform",
        )


class FakeRdsData:
    def __init__(self, *, resuming: int = 0, fail_on: str = "") -> None:
        self.resuming = resuming
        self.fail_on = fail_on
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _record(self, operation: str, kwargs: dict[str, Any]) -> None:
        self.calls.append((operation, kwargs))
        if self.resuming:
            self.resuming -= 1
            error = {"Error": {"Code": "DatabaseResumingException", "Message": "resuming"}}
            raise ClientError(error, operation)

    def execute_statement(self, **kwargs: Any) -> dict[str, Any]:
        self._record("execute_statement", kwargs)
        if self.fail_on and kwargs["sql"] == self.fail_on:
            raise RuntimeError("statement failed")
        return {"records": [[{"booleanValue": True}]]}

    def begin_transaction(self, **kwargs: Any) -> dict[str, str]:
        self._record("begin_transaction", kwargs)
        return {"transactionId": "tx-1"}

    def commit_transaction(self, **kwargs: Any) -> None:
        self._record("commit_transaction", kwargs)

    def rollback_transaction(self, **kwargs: Any) -> None:
        self._record("rollback_transaction", kwargs)


def _data_api(client: FakeRdsData) -> Any:
    return rotation.DataApi(client, "cluster-arn", "master-arn", sleep=lambda _s: None)


def test_data_api_retries_while_the_cluster_resumes():
    client = FakeRdsData(resuming=2)

    assert rotation.role_can_log_in(_data_api(client), "temporal")
    assert len(client.calls) == 3


def test_data_api_rolls_back_a_failed_transaction():
    client = FakeRdsData(fail_on="bad")

    with pytest.raises(RuntimeError):
        _data_api(client).transaction(["good", "bad"], database="agent_platform")

    assert [operation for operation, _ in client.calls][-1] == "rollback_transaction"


def test_postgres_set_secret_sends_the_verifier_through_the_master_secret():
    client = FakeRdsData()

    rotation.postgres_set_secret(_data_api(client), {"username": "temporal", "password": "pw"})

    _, request = client.calls[0]
    assert request["secretArn"] == "master-arn"
    assert request["sql"].startswith('ALTER ROLE "temporal" WITH LOGIN PASSWORD \'SCRAM-SHA-256$')


class FakeElastiCache:
    def __init__(self, statuses: list[str]) -> None:
        self.statuses = statuses
        self.modified: list[dict[str, Any]] = []

    def describe_users(self, UserId: str) -> dict[str, Any]:
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return {
            "Users": [{"UserId": UserId, "Status": status, "Authentication": {"Type": "password"}}]
        }

    def modify_user(self, **kwargs: Any) -> None:
        self.modified.append(kwargs)


def test_elasticache_rotation_keeps_the_current_password_next_to_the_new_one():
    client = FakeElastiCache(["modifying", "active"])
    current = {"username": "langfuse", "password": "old", "user_arn": USER_ARN}
    pending = {**current, "password": "new"}

    rotation.elasticache_set_secret(client, current, pending, sleep=lambda _s: None)

    assert client.modified == [{"UserId": "platform-langfuse", "Passwords": ["old", "new"]}]


def test_waiting_for_a_user_that_never_settles_fails():
    client = FakeElastiCache(["modifying"])

    with pytest.raises(rotation.RotationError):
        rotation.wait_for_active_user(client, "platform-langfuse", timeout=0, sleep=lambda _s: None)


def test_user_id_is_read_from_the_user_arn():
    assert rotation.user_id_from_arn(USER_ARN) == "platform-langfuse"
    with pytest.raises(rotation.RotationError):
        rotation.user_id_from_arn("arn:aws:elasticache:us-east-1:111111111111:cluster:x")
