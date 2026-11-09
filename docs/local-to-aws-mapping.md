# Local-to-AWS mapping

This document maps each local platform component to the AWS resource that hosts it in a deployed environment, and links the Terraform module that provisions that target where one exists. It is a mapping reference. It provisions nothing and changes no runtime code.

The authoritative per-target detail lives in each Terraform module's own README under [infra/terraform](../infra/terraform). This document consolidates and links those modules rather than restating them.

## Deployment posture

In a deployed environment the platform runs inside the target VPC. It reaches the private `bedrock-runtime` interface endpoint with `BEDROCK_CONNECTIVITY_PROVIDER=direct` and no hop. The Fargate jump-host is a laptop path for a test or development account, not a deployed component. A reader mapping the deployed system should treat the jump-host as absent and the connectivity provider as `direct`.

Workloads sign Bedrock inference with a role that carries the `bedrock:InvokeModel` policy from the [bedrock-iam](../infra/terraform/bedrock-iam) module, and private reachability comes from the shared endpoints in the [vpc-endpoints](../infra/terraform/vpc-endpoints) module.

## Component mapping

Locally the activity worker, the review worker, LiteLLM, OPA, the inbound webhook, and the MCP gateway run on the local Kubernetes cluster from the manifests in [deploy/kubernetes](../deploy/kubernetes), and the other components run in Compose. No local pod mounts a host directory, so the inputs each workload reads are the ones it reads in AWS, delivered as images, ConfigMaps, Secrets, and a workspace volume that stands in for the EFS checkouts access point.

| Local component | AWS target | Provisioning |
|---|---|---|
| `postgres` | Aurora PostgreSQL 16 with Serverless v2 capacity in the data tier | [aurora-postgresql](../infra/terraform/aurora-postgresql). Per-service roles planned, see the [roadmap](../ROADMAP.md) |
| `temporal` | Temporal server 1.32.1 service on the platform ECS cluster in the application tier, persisting to Aurora PostgreSQL and reached through an internal NLB that admits only the Temporal client security group | [temporal-control-plane](../infra/terraform/temporal-control-plane), wired by [private-bedrock](../infra/terraform/examples/private-bedrock) |
| `temporal-admin-tools` | One-shot admin-tools task in the management tier that applies the schemas and registers the namespace, run by `inv temporal-deploy` | [temporal-control-plane](../infra/terraform/temporal-control-plane) |
| `temporal-ui` | Temporal UI 2.58.0 service in the management tier behind an internal HTTPS load balancer, reached by operators through `inv temporal-ui` | [temporal-control-plane](../infra/terraform/temporal-control-plane) |
| `db-migrate` | One-shot `alembic upgrade head` task in the management tier as the `platform_migrator` role, run by `inv platform-deploy` | [container-services](../infra/terraform/container-services) |
| `workflow-worker`, `review-worker` | Fargate services in the application tier, each in a blue and a green slot so a new build starts beside the executions pinned to the old one | [container-services](../infra/terraform/container-services) |
| `activity-worker` | One worker per tenant on the EKS cluster, in the `tenant-<tenant>` namespace, which runs the tenant's sandbox and gate pods in `tenant-<tenant>-sandbox` and reaches each on its pod IP. The local cluster renders the same tenant namespaces from the same base manifest | Planned, see the [roadmap](../ROADMAP.md) |
| `schedule-bootstrap` | One-shot task in the management tier that removes the untenanted reap and intake schedules, run by `inv platform-deploy` | [container-services](../infra/terraform/container-services) |
| `coordination-service` | Fargate service in the application tier, reached over HTTPS through the internal load balancer at `coordination.<internal_domain>` | [container-services](../infra/terraform/container-services) |
| `litellm` | Model-proxy service on the container platform, reaching Bedrock through the private endpoint | Bedrock access via [bedrock-iam](../infra/terraform/bedrock-iam) and [vpc-endpoints](../infra/terraform/vpc-endpoints). Service planned, see the [roadmap](../ROADMAP.md) |
| `opa` | Sidecar of the activity worker on the EKS cluster, bound to the pod's loopback so no Service exposes it. The local cluster runs it the same way | Planned, see the [roadmap](../ROADMAP.md) |
| `minio` | Amazon S3 for the evidence and audit object store, the Langfuse uploads, and the rebar ticket store | Evidence bucket via [evidence-store](../infra/terraform/evidence-store), Langfuse bucket via [langfuse-blob-store](../infra/terraform/langfuse-blob-store), trail bucket via [cloudtrail](../infra/terraform/cloudtrail), and rebar ticket store bucket via [rebar-ticket-store](../infra/terraform/rebar-ticket-store). The ticket store syncs through `git-remote-s3`, fetching under a reader role and pushing under a writer role |
| `redis` | ElastiCache for Valkey 9.1 with TLS required and an RBAC user, in the data tier | [elasticache](../infra/terraform/elasticache) |
| `clickhouse` | ClickHouse task in the application tier on an encrypted EFS file system in the data tier, serving only TLS under a Cloud Map name | Storage via [clickhouse-storage](../infra/terraform/clickhouse-storage), task via [container-services](../infra/terraform/container-services) |
| `langfuse-worker`, `langfuse-web` | Fargate services in the application tier against Aurora, ElastiCache, ClickHouse, and the Langfuse bucket. Langfuse web sits behind the internal load balancer, and operators reach it through `inv langfuse-ui` | [container-services](../infra/terraform/container-services) |
| `otel-collector` | Fargate service behind the internal load balancer as the OTLP gateway for the ECS and EKS workloads, exporting traces to AWS X-Ray, metrics to Amazon Managed Service for Prometheus, and generation spans to Langfuse | [container-services](../infra/terraform/container-services) |
| `prometheus` | Amazon Managed Service for Prometheus workspace the collector writes to through SigV4 remote write | [container-services](../infra/terraform/container-services) |
| `tempo` | AWS X-Ray, which the collector writes the traces to | [container-services](../infra/terraform/container-services) |
| `grafana` | Amazon Managed Grafana | Planned, see the [roadmap](../ROADMAP.md) |
| `repo-intelligence`, `repo-index-worker` | Fargate services in the application tier sharing the repository checkouts and index on an encrypted EFS file system. `repo-intelligence` sits behind the internal load balancer | [container-services](../infra/terraform/container-services) |

## Cross-cutting infrastructure

These modules provision the shared AWS infrastructure the components above consume. The [private-bedrock](../infra/terraform/examples/private-bedrock) composition root wires them into one apply for a single account.

| Concern | AWS target | Provisioning module |
|---|---|---|
| Private connectivity | Shared VPC interface and gateway endpoints behind one security group | [vpc-endpoints](../infra/terraform/vpc-endpoints) |
| Container registry | ECR repository with scan-on-push, immutable tags, and a lifecycle policy | [ecr](../infra/terraform/ecr) |
| Audit trail | CloudTrail with its own S3 bucket | [cloudtrail](../infra/terraform/cloudtrail) |
| Bedrock inference authorization | `bedrock:InvokeModel` IAM policy | [bedrock-iam](../infra/terraform/bedrock-iam) |
| Container platform | Shared ECS cluster on Fargate that the platform owns, with encrypted Exec sessions and ephemeral storage | [ecs-cluster](../infra/terraform/ecs-cluster) |
| Kubernetes platform | EKS cluster with a private API endpoint, a platform and a tainted sandbox node group on arm64, network policy enforcement, and Pod Identity roles for the workloads that call AWS | [eks-cluster](../infra/terraform/eks-cluster) |
| Service-to-service HTTPS | One internal ALB under the FIPS TLS policy with host routing to each service, re-encrypting to every task, and a private hosted zone | [container-services](../infra/terraform/container-services) |
| Off-hours schedule | EventBridge Scheduler and SSM Automation that scale the ECS services to zero overnight so Aurora pauses | [off-hours](../infra/terraform/off-hours) |
| Laptop Bedrock path for test accounts | Ephemeral ECS Fargate jump-host reached over ECS Exec, in the management tier | [bedrock-jumphost-fargate](../infra/terraform/bedrock-jumphost-fargate) |
| Network tiers | Public, application, management, and data subnets with per-tier route tables and a data-tier network ACL | [private-bedrock](../infra/terraform/examples/private-bedrock) |
| Outbound internet | NAT instance or NAT Gateway for the application and management tiers | [egress](../infra/terraform/egress) |
| Outbound destination filtering | Route 53 Resolver DNS Firewall allowlist with query logging | [dns-firewall](../infra/terraform/dns-firewall) |
| Terraform state | Versioned, encrypted S3 bucket with S3 native locking | [tf-state](../infra/terraform/tf-state) through [bootstrap](../infra/terraform/bootstrap) |
| Bucket access logging | S3 bucket receiving server access logs from every platform bucket | [access-logs](../infra/terraform/access-logs) through [bootstrap](../infra/terraform/bootstrap) |

## Secret material

The Temporal clients read `TEMPORAL_HOST`, `TEMPORAL_NAMESPACE`, `TEMPORAL_TLS_SERVER_NAME`, and `TEMPORAL_TLS_CA` from the `temporal_client` output of the [private-bedrock](../infra/terraform/examples/private-bedrock) root. Locally the last two are empty and the clients connect without TLS. The Temporal TLS certificates and keys live in Secrets Manager under a key the [temporal-control-plane](../infra/terraform/temporal-control-plane) module creates, and `inv temporal-certs put` writes them.

Local secrets live in `.env` and the `config/local/*.yaml` operator files. In a deployed environment they live in AWS Secrets Manager, provisioned by the [secret-store](../infra/terraform/secret-store) module and injected into each task as environment values. The review and authorship signing keys arrive as `REVIEW_SIGNING_KEY` and `AUTHORSHIP_SIGNING_KEY` in place of the local key files. The LiteLLM master key, the Langfuse keys, the ClickHouse password, the GitHub and Jira tokens and webhook secrets, and the per-service database and cache credentials are the other secret values. The module README lists each one with its rotation. The trust-root bundle at `REVIEW_TRUST_ROOT_PATH` holds only public keys and ships as a non-secret configuration artifact.
