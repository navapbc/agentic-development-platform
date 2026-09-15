# Local-to-AWS mapping

This document maps each local platform component to the AWS resource that hosts it in a deployed environment, and links the Terraform module that provisions that target where one exists. It is a mapping reference. It provisions nothing and changes no runtime code.

The authoritative per-target detail lives in each Terraform module's own README under [infra/terraform](../infra/terraform). This document consolidates and links those modules rather than restating them.

## Deployment posture

In a deployed environment the platform runs inside the target VPC. It reaches the private `bedrock-runtime` interface endpoint with `BEDROCK_CONNECTIVITY_PROVIDER=direct` and no hop. The Fargate jump-host is a laptop path for a test or development account, not a deployed component. A reader mapping the deployed system should treat the jump-host as absent and the connectivity provider as `direct`.

Workloads sign Bedrock inference with a role that carries the `bedrock:InvokeModel` policy from the [bedrock-iam](../infra/terraform/bedrock-iam) module, and private reachability comes from the shared endpoints in the [vpc-endpoints](../infra/terraform/vpc-endpoints) module.

## Component mapping

| Local component | AWS target | Provisioning module |
|---|---|---|
| `postgres` | Amazon RDS for PostgreSQL | Not provisioned yet |
| `temporal`, `temporal-ui` | Temporal control plane on the container platform, or Temporal Cloud | Not provisioned yet |
| `db-migrate` | One-shot migration task on the container platform | Not provisioned yet |
| `workflow-worker`, `activity-worker`, `review-worker` | Worker services on the container platform | Images via [ecr](../infra/terraform/ecr). Service infrastructure not provisioned yet |
| `schedule-bootstrap` | One-shot bootstrap task on the container platform | Not provisioned yet |
| `coordination-service` | Service on the container platform | Not provisioned yet |
| `langgraph-service` | Service on the container platform | Not provisioned yet |
| `litellm` | Model-proxy service on the container platform, reaching Bedrock through the private endpoint | Bedrock access via [bedrock-iam](../infra/terraform/bedrock-iam) and [vpc-endpoints](../infra/terraform/vpc-endpoints). Service infrastructure not provisioned yet |
| `opa` | Policy service on the container platform | Not provisioned yet |
| `minio` | Amazon S3 for the evidence and audit object store and the rebar ticket store | Evidence bucket via [evidence-store](../infra/terraform/evidence-store) and trail bucket via [cloudtrail](../infra/terraform/cloudtrail). Rebar ticket bucket not provisioned yet |
| `redis` | Amazon ElastiCache for Redis | Not provisioned yet |
| `clickhouse` | Analytics store backing Langfuse | Not provisioned yet |
| `langfuse-worker`, `langfuse-web` | Langfuse on the container platform, or Langfuse Cloud | Not provisioned yet |
| `otel-collector` | OpenTelemetry collector on the container platform | Not provisioned yet |
| `prometheus` | Amazon Managed Service for Prometheus | Not provisioned yet |
| `tempo` | Trace store backing the observability stack | Not provisioned yet |
| `grafana` | Amazon Managed Grafana | Not provisioned yet |
| `repo-intelligence`, `repo-index-worker` | Repository-intelligence services on the container platform | Images via [ecr](../infra/terraform/ecr). Service infrastructure not provisioned yet |

## Cross-cutting infrastructure

These modules provision the shared AWS infrastructure the components above consume. The [private-bedrock](../infra/terraform/examples/private-bedrock) composition root wires them into one apply for a single account.

| Concern | AWS target | Provisioning module |
|---|---|---|
| Private connectivity | Shared VPC interface and gateway endpoints behind one security group | [vpc-endpoints](../infra/terraform/vpc-endpoints) |
| Container registry | ECR repository with scan-on-push, immutable tags, and a lifecycle policy | [ecr](../infra/terraform/ecr) |
| Audit trail | CloudTrail with its own S3 bucket | [cloudtrail](../infra/terraform/cloudtrail) |
| Bedrock inference authorization | `bedrock:InvokeModel` IAM policy | [bedrock-iam](../infra/terraform/bedrock-iam) |
| Laptop Bedrock path for test accounts | Ephemeral ECS Fargate jump-host reached over ECS Exec | [bedrock-jumphost-fargate](../infra/terraform/bedrock-jumphost-fargate) |

## Secret material

Local secrets live in `.env` and the `config/local/*.yaml` operator files. In a deployed environment they map to AWS Secrets Manager or SSM Parameter Store, mounted or fetched by the workload. The review signing key at `REVIEW_SIGNING_KEY_PATH`, the authorship signing key at `AUTHORSHIP_SIGNING_KEY_PATH`, the LiteLLM master key, the rebar model API key, and the GitHub and Jira webhook secrets are the secret values a deployment provisions this way. The trust-root bundle at `REVIEW_TRUST_ROOT_PATH` holds only public keys and ships as a non-secret configuration artifact. No module provisions the secret store yet.

## Gaps

The following AWS targets have no Terraform module yet. They are named here so the remaining provisioning work is visible.

- The data tier: RDS for PostgreSQL, ElastiCache for Redis, and the Langfuse analytics store.
- The container-platform services for the workers, the coordination service, the LangGraph service, LiteLLM, OPA, the repository-intelligence services, and Langfuse.
- The Temporal control plane.
- The evidence and audit S3 bucket is provisioned by the [evidence-store](../infra/terraform/evidence-store) module. The rebar ticket S3 bucket is not provisioned yet.
- The observability targets: Amazon Managed Service for Prometheus, Amazon Managed Grafana, and the trace store.
- The secret store for the signing keys, API keys, and webhook secrets.
