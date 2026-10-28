# Local-to-AWS mapping

This document maps each local platform component to the AWS resource that hosts it in a deployed environment, and links the Terraform module that provisions that target where one exists. It is a mapping reference. It provisions nothing and changes no runtime code.

The authoritative per-target detail lives in each Terraform module's own README under [infra/terraform](../infra/terraform). This document consolidates and links those modules rather than restating them.

## Deployment posture

In a deployed environment the platform runs inside the target VPC. It reaches the private `bedrock-runtime` interface endpoint with `BEDROCK_CONNECTIVITY_PROVIDER=direct` and no hop. The Fargate jump-host is a laptop path for a test or development account, not a deployed component. A reader mapping the deployed system should treat the jump-host as absent and the connectivity provider as `direct`.

Workloads sign Bedrock inference with a role that carries the `bedrock:InvokeModel` policy from the [bedrock-iam](../infra/terraform/bedrock-iam) module, and private reachability comes from the shared endpoints in the [vpc-endpoints](../infra/terraform/vpc-endpoints) module.

## Component mapping

| Local component | AWS target | Provisioning |
|---|---|---|
| `postgres` | Aurora PostgreSQL 16 with Serverless v2 capacity in the data tier | [aurora-postgresql](../infra/terraform/aurora-postgresql). Per-service roles planned, see the [roadmap](../ROADMAP.md) |
| `temporal`, `temporal-ui` | Temporal control plane on the container platform, or Temporal Cloud | Planned, see the [roadmap](../ROADMAP.md) |
| `db-migrate` | One-shot migration task on the container platform | Planned, see the [roadmap](../ROADMAP.md) |
| `workflow-worker`, `activity-worker`, `review-worker` | Worker services on the container platform | Images via [ecr](../infra/terraform/ecr). Services planned, see the [roadmap](../ROADMAP.md) |
| `schedule-bootstrap` | One-shot bootstrap task on the container platform | Planned, see the [roadmap](../ROADMAP.md) |
| `coordination-service` | Service on the container platform | Planned, see the [roadmap](../ROADMAP.md) |
| `langgraph-service` | Service on the container platform | Planned, see the [roadmap](../ROADMAP.md) |
| `litellm` | Model-proxy service on the container platform, reaching Bedrock through the private endpoint | Bedrock access via [bedrock-iam](../infra/terraform/bedrock-iam) and [vpc-endpoints](../infra/terraform/vpc-endpoints). Service planned, see the [roadmap](../ROADMAP.md) |
| `opa` | Policy service on the container platform | Planned, see the [roadmap](../ROADMAP.md) |
| `minio` | Amazon S3 for the evidence and audit object store, the Langfuse uploads, and the rebar ticket store | Evidence bucket via [evidence-store](../infra/terraform/evidence-store), Langfuse bucket via [langfuse-blob-store](../infra/terraform/langfuse-blob-store), and trail bucket via [cloudtrail](../infra/terraform/cloudtrail). Rebar ticket bucket planned, see the [roadmap](../ROADMAP.md) |
| `redis` | ElastiCache for Valkey 9.1 with TLS required and an RBAC user, in the data tier | [elasticache](../infra/terraform/elasticache) |
| `clickhouse` | ClickHouse task on the container platform, backed by an encrypted EFS file system in the data tier | Storage via [clickhouse-storage](../infra/terraform/clickhouse-storage). Task planned, see the [roadmap](../ROADMAP.md) |
| `langfuse-worker`, `langfuse-web` | Langfuse on the container platform, or Langfuse Cloud | Planned, see the [roadmap](../ROADMAP.md) |
| `otel-collector` | OpenTelemetry collector on the container platform | Planned, see the [roadmap](../ROADMAP.md) |
| `prometheus` | Amazon Managed Service for Prometheus | Planned, see the [roadmap](../ROADMAP.md) |
| `tempo` | Trace store backing the observability stack | Planned, see the [roadmap](../ROADMAP.md) |
| `grafana` | Amazon Managed Grafana | Planned, see the [roadmap](../ROADMAP.md) |
| `repo-intelligence`, `repo-index-worker` | Repository-intelligence services on the container platform | Images via [ecr](../infra/terraform/ecr). Services planned, see the [roadmap](../ROADMAP.md) |

## Cross-cutting infrastructure

These modules provision the shared AWS infrastructure the components above consume. The [private-bedrock](../infra/terraform/examples/private-bedrock) composition root wires them into one apply for a single account.

| Concern | AWS target | Provisioning module |
|---|---|---|
| Private connectivity | Shared VPC interface and gateway endpoints behind one security group | [vpc-endpoints](../infra/terraform/vpc-endpoints) |
| Container registry | ECR repository with scan-on-push, immutable tags, and a lifecycle policy | [ecr](../infra/terraform/ecr) |
| Audit trail | CloudTrail with its own S3 bucket | [cloudtrail](../infra/terraform/cloudtrail) |
| Bedrock inference authorization | `bedrock:InvokeModel` IAM policy | [bedrock-iam](../infra/terraform/bedrock-iam) |
| Laptop Bedrock path for test accounts | Ephemeral ECS Fargate jump-host reached over ECS Exec, in the management tier | [bedrock-jumphost-fargate](../infra/terraform/bedrock-jumphost-fargate) |
| Network tiers | Public, application, management, and data subnets with per-tier route tables and a data-tier network ACL | [private-bedrock](../infra/terraform/examples/private-bedrock) |
| Outbound internet | NAT instance or NAT Gateway for the application and management tiers | [egress](../infra/terraform/egress) |
| Outbound destination filtering | Route 53 Resolver DNS Firewall allowlist with query logging | [dns-firewall](../infra/terraform/dns-firewall) |
| Terraform state | Versioned, encrypted S3 bucket with S3 native locking | [tf-state](../infra/terraform/tf-state) through [bootstrap](../infra/terraform/bootstrap) |
| Bucket access logging | S3 bucket receiving server access logs from every platform bucket | [access-logs](../infra/terraform/access-logs) through [bootstrap](../infra/terraform/bootstrap) |

## Secret material

Local secrets live in `.env` and the `config/local/*.yaml` operator files. In a deployed environment they live in AWS Secrets Manager, provisioned by the [secret-store](../infra/terraform/secret-store) module and injected into each task as environment values. The review and authorship signing keys arrive as `REVIEW_SIGNING_KEY` and `AUTHORSHIP_SIGNING_KEY` in place of the local key files. The LiteLLM master key, the Langfuse keys, the ClickHouse password, the GitHub and Jira tokens and webhook secrets, and the per-service database and cache credentials are the other secret values. The module README lists each one with its rotation. The trust-root bundle at `REVIEW_TRUST_ROOT_PATH` holds only public keys and ships as a non-secret configuration artifact.
