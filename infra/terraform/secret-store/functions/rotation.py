"""Secrets Manager rotation and role bootstrap for the platform data-tier credentials.

``postgres_handler`` rotates a per-service Aurora PostgreSQL role. Invoked with
``{"action": "bootstrap"}``, it creates the per-service roles and databases instead. Both
paths run SQL through the RDS Data API as the RDS-managed master user, so the function
needs no network path to the database port and no database driver. Passwords are sent as
SCRAM-SHA-256 verifiers, so the statement text that PostgreSQL logs under
``log_statement = ddl`` never carries a password.

``elasticache_handler`` rotates a Valkey RBAC user through the ElastiCache ``ModifyUser``
API. The user keeps its current password next to the new one, so a consumer that has not
restarted yet keeps connecting. The next rotation drops the older password.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger()
log.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

AWSCURRENT = "AWSCURRENT"
AWSPENDING = "AWSPENDING"
PASSWORD_LENGTH = 40
SCRAM_ITERATIONS = 4096
WAIT_TIMEOUT_SECONDS = 600.0
POLL_SECONDS = 10.0

PLATFORM_MIGRATOR = "platform_migrator"
PLATFORM_APP = "platform_app"
TEMPORAL = "temporal"
LANGFUSE = "langfuse"
ROLE_KEYS = (PLATFORM_MIGRATOR, PLATFORM_APP, TEMPORAL, LANGFUSE)

_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

Credential = dict[str, Any]
Sleep = Callable[[float], None]


class RotationError(RuntimeError):
    """The secret or its target is not in a state the requested step can act on."""


def quote_identifier(name: str) -> str:
    if not _IDENTIFIER.match(name):
        raise ValueError(f"{name!r} is not a lowercase PostgreSQL identifier")
    return f'"{name}"'


def scram_sha256_verifier(
    password: str, *, salt: bytes | None = None, iterations: int = SCRAM_ITERATIONS
) -> str:
    """Return the SCRAM-SHA-256 verifier PostgreSQL stores for ``password`` (RFC 7677)."""
    salt = salt if salt is not None else secrets.token_bytes(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()

    def encode(raw: bytes) -> str:
        return base64.b64encode(raw).decode("ascii")

    return f"SCRAM-SHA-256${iterations}:{encode(salt)}${encode(stored_key)}:{encode(server_key)}"


def password_statement(username: str, password: str) -> str:
    verifier = scram_sha256_verifier(password)
    return f"ALTER ROLE {quote_identifier(username)} WITH LOGIN PASSWORD '{verifier}'"


def _secret_json(client: Any, arn: str, stage: str, token: str | None = None) -> Credential:
    kwargs = {"SecretId": arn, "VersionStage": stage}
    if token is not None:
        kwargs["VersionId"] = token
    return json.loads(client.get_secret_value(**kwargs)["SecretString"])


def create_secret(client: Any, arn: str, token: str) -> None:
    current = _secret_json(client, arn, AWSCURRENT)
    try:
        _secret_json(client, arn, AWSPENDING, token)
        return
    except client.exceptions.ResourceNotFoundException:
        pass
    password = client.get_random_password(PasswordLength=PASSWORD_LENGTH, ExcludePunctuation=True)[
        "RandomPassword"
    ]
    client.put_secret_value(
        SecretId=arn,
        ClientRequestToken=token,
        SecretString=json.dumps({**current, "password": password}),
        VersionStages=[AWSPENDING],
    )
    log.info("staged version %s of %s as AWSPENDING", token, arn)


def finish_secret(client: Any, arn: str, token: str) -> None:
    stages = client.describe_secret(SecretId=arn)["VersionIdsToStages"]
    current = next((version for version, names in stages.items() if AWSCURRENT in names), None)
    if current == token:
        return
    client.update_secret_version_stage(
        SecretId=arn,
        VersionStage=AWSCURRENT,
        MoveToVersionId=token,
        RemoveFromVersionId=current,
    )
    log.info("promoted version %s of %s to AWSCURRENT", token, arn)


def rotate(
    event: Mapping[str, Any],
    client: Any,
    *,
    set_secret: Callable[[Credential, Credential], None],
    test_secret: Callable[[Credential], None],
) -> None:
    arn, token, step = event["SecretId"], event["ClientRequestToken"], event["Step"]
    metadata = client.describe_secret(SecretId=arn)
    if not metadata.get("RotationEnabled"):
        raise RotationError(f"rotation is not enabled for {arn}")
    stages = metadata.get("VersionIdsToStages", {})
    if token not in stages:
        raise RotationError(f"version {token} is not a version of {arn}")
    if AWSCURRENT in stages[token]:
        return
    if AWSPENDING not in stages[token]:
        raise RotationError(f"version {token} of {arn} is not staged as AWSPENDING")

    if step == "createSecret":
        create_secret(client, arn, token)
    elif step == "setSecret":
        current = _secret_json(client, arn, AWSCURRENT)
        set_secret(current, _secret_json(client, arn, AWSPENDING, token))
    elif step == "testSecret":
        test_secret(_secret_json(client, arn, AWSPENDING, token))
    elif step == "finishSecret":
        finish_secret(client, arn, token)
    else:
        raise RotationError(f"unknown rotation step {step!r}")


class DataApi:
    """Run SQL on the cluster through the RDS Data API as the master user.

    A Serverless v2 cluster paused at zero capacity answers the first call with
    DatabaseResumingException while it resumes, so each call retries until the cluster
    accepts it or the wait times out.
    """

    def __init__(
        self,
        client: Any,
        cluster_arn: str,
        secret_arn: str,
        *,
        resume_timeout: float = WAIT_TIMEOUT_SECONDS,
        sleep: Sleep = time.sleep,
    ) -> None:
        self._client = client
        self._cluster_arn = cluster_arn
        self._secret_arn = secret_arn
        self._resume_timeout = resume_timeout
        self._sleep = sleep

    def _call(self, operation: str, **kwargs: Any) -> Mapping[str, Any]:
        deadline = time.monotonic() + self._resume_timeout
        request = {"resourceArn": self._cluster_arn, "secretArn": self._secret_arn, **kwargs}
        while True:
            try:
                return getattr(self._client, operation)(**request)
            except ClientError as exc:
                resuming = exc.response.get("Error", {}).get("Code") == "DatabaseResumingException"
                if not resuming or time.monotonic() >= deadline:
                    raise
                self._sleep(POLL_SECONDS)

    def execute(
        self, sql: str, *, database: str, parameters: Sequence[Mapping[str, Any]] = ()
    ) -> list[list[Mapping[str, Any]]]:
        response = self._call(
            "execute_statement", database=database, sql=sql, parameters=list(parameters)
        )
        return list(response.get("records", []))

    def transaction(self, statements: Sequence[str], *, database: str) -> None:
        transaction_id = self._call("begin_transaction", database=database)["transactionId"]
        try:
            for sql in statements:
                self._call(
                    "execute_statement", database=database, sql=sql, transactionId=transaction_id
                )
        except Exception:
            self._call("rollback_transaction", transactionId=transaction_id)
            raise
        self._call("commit_transaction", transactionId=transaction_id)


def role_can_log_in(data_api: DataApi, username: str) -> bool:
    records = data_api.execute(
        "SELECT rolcanlogin FROM pg_roles WHERE rolname = :name",
        database="postgres",
        parameters=[{"name": "name", "value": {"stringValue": username}}],
    )
    return bool(records) and bool(records[0][0].get("booleanValue"))


def postgres_set_secret(data_api: DataApi, pending: Credential) -> None:
    statement = password_statement(pending["username"], pending["password"])
    data_api.execute(statement, database="postgres")


def postgres_test_secret(data_api: DataApi, pending: Credential) -> None:
    if not role_can_log_in(data_api, pending["username"]):
        raise RotationError(f"role {pending['username']!r} does not exist or cannot log in")


@dataclass(frozen=True)
class BootstrapStep:
    database: str
    statements: tuple[str, ...]
    transactional: bool = False


def role_statements(username: str, password: str, *, exists: bool) -> tuple[str, ...]:
    """Create the role when absent, set its password, and let the master user act as it.

    PostgreSQL 16 requires that membership before the master user can create a database
    owned by the role or run the platform schema grants as the migrator.
    """
    role = quote_identifier(username)
    create = () if exists else (f"CREATE ROLE {role} LOGIN",)
    return (*create, password_statement(username, password), f"GRANT {role} TO CURRENT_USER")


def database_statements(database: str, owner: str, *, exists: bool) -> tuple[str, ...]:
    name, owner_role = quote_identifier(database), quote_identifier(owner)
    if exists:
        create = f"ALTER DATABASE {name} OWNER TO {owner_role}"
    else:
        create = f"CREATE DATABASE {name} OWNER {owner_role}"
    return (create, f"REVOKE ALL ON DATABASE {name} FROM PUBLIC")


def platform_database_statements(database: str, *, migrator: str, app: str) -> tuple[str, ...]:
    name = quote_identifier(database)
    return (
        f"REVOKE ALL ON DATABASE {name} FROM PUBLIC",
        f"GRANT CONNECT, TEMPORARY, CREATE ON DATABASE {name} TO {quote_identifier(migrator)}",
        f"GRANT CONNECT, TEMPORARY ON DATABASE {name} TO {quote_identifier(app)}",
    )


def platform_schema_statements(schema: str, *, migrator: str, app: str) -> tuple[str, ...]:
    """Grant the application role DML on the migrator's schema and read on alembic_version.

    The statements run as the migrator, so it owns the schema and every table, holds the
    only DDL rights, and is the grantor the closing REVOKE needs. Alembic finds the
    pre-created version table and uses it, so the application role never gains write
    access to it.
    """
    name = quote_identifier(schema)
    app_role = quote_identifier(app)
    version_table = f"{name}.alembic_version"
    return (
        f"SET LOCAL ROLE {quote_identifier(migrator)}",
        f"CREATE SCHEMA IF NOT EXISTS {name}",
        f"CREATE TABLE IF NOT EXISTS {version_table} ("
        "version_num VARCHAR(32) NOT NULL, "
        "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))",
        f"GRANT USAGE ON SCHEMA {name} TO {app_role}",
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {name} "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {app_role}",
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {name} "
        f"GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO {app_role}",
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {name} TO {app_role}",
        f"GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA {name} TO {app_role}",
        f"REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON {version_table} FROM {app_role}",
    )


def bootstrap_plan(
    credentials: Mapping[str, Credential],
    *,
    existing_roles: frozenset[str],
    existing_databases: frozenset[str],
    platform_database: str,
    platform_schema: str,
) -> list[BootstrapStep]:
    missing = [key for key in ROLE_KEYS if key not in credentials]
    if missing:
        raise RotationError(f"bootstrap needs credentials for {', '.join(missing)}")
    names = {key: credentials[key]["username"] for key in ROLE_KEYS}
    migrator, app = names[PLATFORM_MIGRATOR], names[PLATFORM_APP]

    role_sql: list[str] = []
    for key in ROLE_KEYS:
        exists = names[key] in existing_roles
        role_sql.extend(role_statements(names[key], credentials[key]["password"], exists=exists))

    service_databases = {
        "temporal": names[TEMPORAL],
        "temporal_visibility": names[TEMPORAL],
        "langfuse": names[LANGFUSE],
    }
    database_sql: list[str] = []
    for database, owner in service_databases.items():
        exists = database in existing_databases
        database_sql.extend(database_statements(database, owner, exists=exists))
    database_sql.extend(platform_database_statements(platform_database, migrator=migrator, app=app))

    steps = [
        BootstrapStep("postgres", tuple(role_sql)),
        BootstrapStep("postgres", tuple(database_sql)),
    ]
    steps.extend(
        BootstrapStep(database, ("CREATE EXTENSION IF NOT EXISTS pgaudit",))
        for database in (platform_database, *service_databases)
    )
    steps.append(
        BootstrapStep(
            platform_database,
            platform_schema_statements(platform_schema, migrator=migrator, app=app),
            transactional=True,
        )
    )
    return steps


def _names(data_api: DataApi, sql: str) -> frozenset[str]:
    records = data_api.execute(sql, database="postgres")
    return frozenset(str(row[0].get("stringValue")) for row in records)


def existing_roles(data_api: DataApi) -> frozenset[str]:
    return _names(data_api, "SELECT rolname FROM pg_roles")


def existing_databases(data_api: DataApi) -> frozenset[str]:
    return _names(data_api, "SELECT datname FROM pg_database")


def run_bootstrap(
    data_api: DataApi,
    secrets_client: Any,
    role_secret_arns: Mapping[str, str],
    *,
    platform_database: str,
    platform_schema: str,
) -> dict[str, Any]:
    credentials = {
        key: _secret_json(secrets_client, arn, AWSCURRENT) for key, arn in role_secret_arns.items()
    }
    plan = bootstrap_plan(
        credentials,
        existing_roles=existing_roles(data_api),
        existing_databases=existing_databases(data_api),
        platform_database=platform_database,
        platform_schema=platform_schema,
    )
    for step in plan:
        if step.transactional:
            data_api.transaction(step.statements, database=step.database)
            continue
        for sql in step.statements:
            data_api.execute(sql, database=step.database)
    roles = sorted(credential["username"] for credential in credentials.values())
    log.info("bootstrap configured roles %s", ", ".join(roles))
    return {"roles": roles, "steps": len(plan)}


def postgres_handler(event: Mapping[str, Any], _context: Any = None) -> dict[str, Any] | None:
    secrets_client = boto3.client("secretsmanager")
    data_api = DataApi(
        boto3.client("rds-data"), os.environ["CLUSTER_ARN"], os.environ["MASTER_SECRET_ARN"]
    )
    if event.get("action") == "bootstrap":
        return run_bootstrap(
            data_api,
            secrets_client,
            json.loads(os.environ["ROLE_SECRET_ARNS"]),
            platform_database=os.environ["PLATFORM_DATABASE"],
            platform_schema=os.environ["PLATFORM_SCHEMA"],
        )
    rotate(
        event,
        secrets_client,
        set_secret=lambda _current, pending: postgres_set_secret(data_api, pending),
        test_secret=lambda pending: postgres_test_secret(data_api, pending),
    )
    return None


def user_id_from_arn(user_arn: str) -> str:
    _, separator, user_id = user_arn.rpartition(":user:")
    if not separator or not user_id:
        raise RotationError(f"{user_arn!r} is not an ElastiCache user ARN")
    return user_id


def wait_for_active_user(
    client: Any,
    user_id: str,
    *,
    timeout: float = WAIT_TIMEOUT_SECONDS,
    sleep: Sleep = time.sleep,
) -> Mapping[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        user = client.describe_users(UserId=user_id)["Users"][0]
        if user["Status"] == "active":
            return user
        if time.monotonic() >= deadline:
            raise RotationError(f"user {user_id} is still {user['Status']} after {timeout}s")
        sleep(POLL_SECONDS)


def elasticache_set_secret(
    client: Any, current: Credential, pending: Credential, *, sleep: Sleep = time.sleep
) -> None:
    user_id = user_id_from_arn(pending["user_arn"])
    wait_for_active_user(client, user_id, sleep=sleep)
    client.modify_user(UserId=user_id, Passwords=[current["password"], pending["password"]])
    wait_for_active_user(client, user_id, sleep=sleep)


def elasticache_test_secret(client: Any, pending: Credential, *, sleep: Sleep = time.sleep) -> None:
    user = wait_for_active_user(client, user_id_from_arn(pending["user_arn"]), sleep=sleep)
    if user.get("Authentication", {}).get("Type") != "password":
        raise RotationError(f"user {user['UserId']} does not authenticate with a password")


def elasticache_handler(event: Mapping[str, Any], _context: Any = None) -> None:
    elasticache = boto3.client("elasticache")
    rotate(
        event,
        boto3.client("secretsmanager"),
        set_secret=lambda current, pending: elasticache_set_secret(elasticache, current, pending),
        test_secret=lambda pending: elasticache_test_secret(elasticache, pending),
    )
