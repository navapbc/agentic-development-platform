# secret-store

Reusable module that holds the platform's secret material in AWS Secrets Manager. Every secret is encrypted with a dedicated KMS key, is readable only by the roles passed in for it, and is rotated on a schedule where the target system can accept a new value without a release. Nothing is created unless `create_secret_store = true`.

## What it provisions

- A KMS key with yearly automatic key rotation that encrypts the secrets, the rotation function environment, and the rotation function logs.
- One Secrets Manager secret per entry in the catalog below, named `<name_prefix>/<group>/<secret>`.
- A resource policy on every secret, including the cache user secret from the `elasticache` module. It allows `GetSecretValue` for the roles in `secret_readers` and denies it to every other principal except the secret's rotation function.
- With `create_database_credentials`, one credential secret per service role, a PostgreSQL rotation function, and the role bootstrap that runs through the same function.
- With `rotate_elasticache_user`, an ElastiCache rotation function attached to the cache user secret.
- With `restart_services`, an SSM Automation document and one EventBridge rule per secret that redeploy the ECS services that read the secret.

## Secret catalog

Generated secrets get a random value from Terraform on the first apply. Terraform writes the value through the write-only `secret_string_wo` argument from an ephemeral resource, so the value never enters the plan or state. Passwords come from the Secrets Manager `GetRandomPassword` API. The 64-character hex key comes from `random_bytes`.

| Key | Group | Consumer setting | Rotation |
|---|---|---|---|
| `litellm_master_key` | generated | `LITELLM_MASTER_KEY`, `REBAR_LLM_API_KEY` | Manual, by generation |
| `review_attestation_secret` | generated | `REVIEW_ATTESTATION_SECRET` | Manual, by generation |
| `langfuse_nextauth_secret` | generated | `NEXTAUTH_SECRET` | Manual, by generation |
| `langfuse_salt` | generated | `SALT` | Manual, by generation |
| `langfuse_encryption_key` | generated | `ENCRYPTION_KEY` | None, see below |
| `langfuse_public_key` | generated | `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_INIT_PROJECT_PUBLIC_KEY` | Manual, by generation |
| `langfuse_secret_key` | generated | `LANGFUSE_SECRET_KEY`, `LANGFUSE_INIT_PROJECT_SECRET_KEY` | Manual, by generation |
| `clickhouse_password` | generated | `CLICKHOUSE_PASSWORD` | Manual, by generation |
| `github_token` | external | `GITHUB_TOKEN` | At the issuer |
| `github_webhook_secret` | external | `GITHUB_WEBHOOK_SECRET` | At the issuer |
| `jira_api_token` | external | `JIRA_API_TOKEN` | At the issuer |
| `jira_pat` | external | `JIRA_PAT` | At the issuer |
| `jira_webhook_secret` | external | `JIRA_WEBHOOK_SECRET` | At the issuer |
| `slack_webhook_url` | external | `SLACK_WEBHOOK_URL` | At the issuer |
| `review_signing_key` | external | `REVIEW_SIGNING_KEY` | With a trust-root change |
| `authorship_signing_key` | external | `AUTHORSHIP_SIGNING_KEY` | With an identity key change |
| `database_platform_migrator` | database | `ALEMBIC_DATABASE_URL` user and password | Automatic |
| `database_platform_app` | database | `DATABASE_URL` user and password | Automatic |
| `database_temporal` | database | Temporal `POSTGRES_USER` and `POSTGRES_PWD` | Automatic |
| `database_langfuse` | database | Langfuse `DATABASE_URL` user and password | Automatic |
| `elasticache_user` | the `elasticache` module | `REDIS_USERNAME` and `REDIS_AUTH` | Automatic |

External secrets are created empty. Set each one before the first deployment that reads it, for example `aws secretsmanager put-secret-value --secret-id agentic-platform/external/github-token --secret-string file://token.txt`. ECS cannot start a task that references a secret with no value.

The trust-root bundle holds only public keys and stays in configuration. The MinIO and evidence-store access keys are not provisioned, because the deployed workloads use their IAM roles.

## Access

`secret_readers` maps a secret key to the role ARNs that read it. For a secret that ECS injects into a task, pass the task execution role, because ECS reads the value when the task starts. The key policy lets those roles decrypt only through Secrets Manager. A secret with no readers denies `GetSecretValue` to everyone except its rotation function, which suits an external secret before its consumer exists. An operator still writes a value with `PutSecretValue`.

## Automatic rotation

The database and cache credentials rotate on `rotation_schedule_expression`, which defaults to `cron(0 6 ? 1/3 SUN#1 *)`. That is the first Sunday of January, April, July, and October at 06:00 UTC, so the interval is at most 92 days. Secrets Manager starts the rotation at some point in the `rotation_window`, which defaults to three hours. Enabling rotation does not rotate at once, so the first rotation waits for the schedule.

The rotation functions run in the management tier. They have no ingress, and their only egress is HTTPS to the shared interface endpoints. They reach Secrets Manager, the RDS Data API, and ElastiCache through the `secretsmanager`, `rds-data`, and `elasticache` endpoints, so they need no route to the internet and no path to the database or cache ports.

| Target | Mechanism | Effect on running consumers |
|---|---|---|
| PostgreSQL service roles | The function runs `ALTER ROLE ... PASSWORD` through the Data API as the RDS-managed master user. It sends a SCRAM-SHA-256 verifier, so the statement that `log_statement = ddl` writes to the cluster log carries no password. | Open connections stay open. A consumer that opens a new connection with the old password is refused until its restart. |
| ElastiCache user | The function calls `ModifyUser` with the current and the new password. The next rotation drops the older one. | Consumers keep connecting with either password until their restart. |

The RDS-managed master secret rotates every seven days through RDS itself and is used only by the bootstrap and the rotation function.

## Role bootstrap

The PostgreSQL function also creates the service roles and databases when it is invoked with `{"action": "bootstrap"}`. Run it with `inv db-bootstrap-roles --function-name <role_bootstrap_function_name>` once after the first apply, before the first migration and before the first scheduled rotation. The bootstrap is idempotent and sets every role password to the current value of its secret, so it also repairs a role after a database restore.

| Role | Gets |
|---|---|
| `platform_migrator` | Owns the `agent_platform` schema and every table in it, and holds `CREATE` on the database. It is the only role with DDL rights on the platform schema. |
| `platform_app` | `CONNECT` on `agent_platform`, `SELECT`, `INSERT`, `UPDATE`, and `DELETE` on the schema's tables, sequence use, and only `SELECT` on `alembic_version`, which the startup schema check reads. |
| `temporal` | Owns the `temporal` and `temporal_visibility` databases. Run the Temporal schema tool with `setup-schema` and `update-schema`, because the databases already exist. |
| `langfuse` | Owns the `langfuse` database, where its migrations run. |

The bootstrap revokes `PUBLIC` access on each of these databases, so a role connects only to its own database. It runs `CREATE EXTENSION pgaudit` in each database. It pre-creates `agent_platform.alembic_version` as the migrator, so the application role never gains write access to it when Alembic later finds and uses the table. The bootstrap expects a cluster on which the platform migrations have not run as another role. It requires PostgreSQL 15 or later.

## Restart after a change

Each service reads its secrets once, when its task starts. `restart_services` maps a secret key to up to five ECS service ARNs. For each listed secret, an EventBridge rule starts the restart automation on a `RotationSucceeded` event or on a `PutSecretValue` that does not stage a pending version, which is an operator update. The automation calls `UpdateService` with `forceNewDeployment`, so ECS starts tasks that read the new value before it stops the old ones. The rules read CloudTrail management events, so the account needs a trail that records them, such as the one the `cloudtrail` module creates.

## Manual rotation runbook

| Secret | Procedure |
|---|---|
| Database and cache credentials | `aws secretsmanager rotate-secret --secret-id <arn>`. The restart rule redeploys the consumers when the rotation succeeds. |
| Generated secrets | Increment the secret's entry in `generated_secret_versions` and apply. Terraform writes a new value as an operator update, so the restart rule redeploys the consumers. Rotating `review_attestation_secret` makes earlier HMAC attestations fail verification, so keep the old value with the evidence it verified. Rotating the Langfuse project keys requires the same keys in the Langfuse project. |
| External secrets | Issue the new token or webhook secret at GitHub, Jira, or Slack, then `aws secretsmanager put-secret-value`. Revoke the old one at the issuer after the consumers restart. |
| Signing keys | Add the new public key to the trust root or identity, write the private key with `put-secret-value`, then revoke the old key. |
| `langfuse_encryption_key` | Not rotated. Langfuse encrypts stored data with it and has no re-encryption path, so a new key makes stored data unreadable. |

Rotate a credential at once, outside the schedule, when it may be exposed, when a person with access to it leaves or changes role, and when the membership of a group that shares it changes.

## FedRAMP Moderate

The schedule meets the organization-defined period that IA-5(f) asks for, and the event triggers above meet the change events of IA-5(f) and IA-5(i). These are service authenticators, not user memorized secrets, so the IA-5(1) guidance against forced periodic change does not apply. Encryption under a customer managed key with yearly key rotation and access limited per secret support IA-5(7), SC-12, SC-28, and AC-6.

## Usage

```hcl
module "secret_store" {
  source = "../secret-store"

  create_secret_store = true
  name_prefix         = "agentic-platform"
  region              = "us-east-1"

  create_database_credentials = true
  database_cluster_arn        = module.aurora_postgresql.cluster_arn
  database_master_secret_arn  = module.aurora_postgresql.master_user_secret_arn

  rotate_elasticache_user     = true
  elasticache_user_secret_arn = module.elasticache.user_secret_arn
  elasticache_user_arn        = module.elasticache.user_arn

  vpc_id                      = aws_vpc.this.id
  subnet_ids                  = aws_subnet.management[*].id
  endpoints_security_group_id = module.vpc_endpoints.security_group_id

  secret_readers = {
    database_temporal = [aws_iam_role.temporal_execution.arn]
  }
  restart_services = {
    database_temporal = [aws_ecs_service.temporal.id]
  }
}
```

The cluster needs `enable_data_api = true` on the `aurora-postgresql` module. Pass `kms_key_arn` to the `elasticache` module as `secret_kms_key_arn`, so the cache secret uses the same key.
