---
title: Container-Platform Services Terraform - Plan
type: feat
date: 2026-11-05
origin: .agent-context/roadmap-jira.md
ticket: ADP-61
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: roadmap
execution: infra
---

# Container-Platform Services Terraform - Plan

## Goal Capsule

Objective. A deployed environment runs the platform containers on two AWS container platforms. ECS on Fargate runs the workers and services that launch no pods, and EKS runs the workloads that orchestrate pods through the Kubernetes API. Each workload has a least-privilege role, secrets injected by ARN, private placement, encrypted transport, and CloudWatch log export.

Means. A toggleable `container-services` module runs the ECS workloads on the platform ECS cluster behind one internal HTTPS load balancer that re-encrypts to every task. An `eks-cluster` module and a production kustomize overlay run the EKS workloads. Both consume the Temporal control plane, the data tier, the secret store, and the image repositories.

Product authority. ADP-61 in Jira, from the v0.3.0 section of `.agent-context/roadmap-jira.md`. ADP-136 covers ECS with unit sub-tasks ADP-154 to ADP-159. ADP-137 covers EKS. ADP-139 and ADP-140 precede ADP-137.

Stop conditions. The ECS workloads run in the sandbox account. Each worker polls the deployed Temporal server as a Worker Deployment version over TLS, review-worker signs a verdict, a trace lands in Langfuse and in X-Ray, a rotation redeploys the services, and the off-hours schedule lets the Aurora cluster pause. `terraform validate`, `inv terraform-test`, `inv ci`, and `inv sast` pass. The EKS units are recorded when ADP-137 starts.

## Product Contract

### Summary

The platform containers run in compose locally, and no Terraform provisions them in a deployed environment. ECS on Fargate hosts `workflow-worker`, `review-worker`, `coordination-service`, `repo-intelligence`, `repo-index-worker`, `langfuse-web`, `langfuse-worker`, `clickhouse`, `otel-collector`, and the one-shot `db-migrate` and `schedule-bootstrap` tasks. EKS hosts `activity-worker` with an OPA sidecar, `litellm`, `inbound-webhook`, `mcp-gateway`, and the sandbox, gate, checks, and scanner pods. `langgraph-service` is not deployed.

### Problem Frame

The compute layer runs the platform code and holds the credentials that reach Bedrock, the data tier, the source control and ticket systems, and the signing keys. FedRAMP Medium expects AC-6 least privilege per workload, SC-7 boundary protection, SC-8 transmission confidentiality including service-to-service traffic, SC-13 FIPS-validated cryptography, SC-28 protection at rest, IA-5 authenticator management, and AU log export. The platform's responsibility is the Terraform and the images that provision each workload with those settings.

### Requirements

- R1. Every image an ECS task runs is in an ECR repository in the account, built from the pinned sources or mirrored by digest.
- R2. Each ECS service has a task role scoped to the AWS actions it calls, an execution role scoped to its image, log group, and secrets, and a security group scoped to its declared sources and destinations.
- R3. Each secret reaches a task by ARN through the execution role, and no task definition holds a secret value.
- R4. Services run in the application tier, the one-shot tasks in the management tier, and nothing in the data tier.
- R5. Service-to-service traffic is encrypted. Callers on ECS and EKS reach `coordination-service`, `repo-intelligence`, `otel-collector`, and `langfuse-web` through one internal HTTPS load balancer under the FIPS policy, which re-encrypts to each task.
- R6. Every Temporal client attaches the Temporal client security group and connects over TLS with the control plane outputs. The workers register as Worker Deployment versions of the build in their image.
- R7. Database clients connect with `sslmode=verify-full` against the RDS CA bundle, and the pools of every ECS and EKS client fit within `max_connections` at the maximum Aurora capacity.
- R8. `db-migrate` runs as the migrator role before the services move to a new build, and `schedule-bootstrap` runs after them.
- R9. ClickHouse runs on the `clickhouse-storage` access point, and Langfuse runs against Aurora, ElastiCache over TLS, and the `langfuse-blob-store` bucket.
- R10. The collector is the OTLP gateway for ECS and EKS and exports traces to X-Ray and metrics to Amazon Managed Service for Prometheus behind a toggle.
- R11. An off-hours schedule scales every ECS service that holds a database connection to zero and restores it at wake, and a credential rotation redeploys every service that reads the credential.
- R12. The module is gated by `create_container_services`, provisions nothing when off, and is wired into `private-bedrock`.

### Scope Boundaries

- The Temporal server and UI are ADP-97, which is complete. This plan consumes its client outputs.
- The off-hours scale-down of the Temporal server and the database pause ordering are ADP-146.
- Dashboards and alarms on the exported telemetry are ADP-95.
- The activity worker, LiteLLM, and OPA move into the local cluster in ADP-139, and MCP gateway client authentication is ADP-140. Both precede ADP-137.
- `inbound-webhook` runs on EKS behind the internet-facing ALB in ADP-137.

### Sources

- `compose.yaml` and `compose.override.yaml` for the images, pins, ports, healthchecks, and environment of each container.
- `services/orchestration/worker.py`, whose `WORKER_ROLE` selects the workflows, activities, or review activities a worker registers, and whose startup runs the schema check, the review signing preflight, and the evidence store preflight.
- `observability/telemetry.py`, whose OTLP exporter speaks gRPC by default, and `observability/otel/otel-collector-config.yaml`.
- `infra/terraform/secret-store`, `aurora-postgresql`, `elasticache`, `clickhouse-storage`, `langfuse-blob-store`, `ecs-cluster`, and `temporal-control-plane` for the inputs this plan consumes.

## Planning Contract

### Key Technical Decisions

- KTD1. One `container-services` module with one task role, execution role, security group, and log group per service. Governs R2. A per-service role is the unit of least privilege. Only review-worker receives `bedrock:InvokeModel`, and only the collector receives the X-Ray and remote write actions.
- KTD2. One shared internal ALB with host-based routing under `ELBSecurityPolicy-TLS13-1-2-FIPS-2023-04` in place of an ALB per service, and an HTTPS target group per service so the load balancer re-encrypts to the task. Governs R5. The EKS callers need a load balancer either way, ECS Service Connect TLS would need AWS Private CA and would not cover EKS callers, and one ALB costs one hourly charge. A Route 53 private hosted zone names each service, and DNS Firewall allows the zone.
- KTD3. Each task serves TLS with a certificate issued when the task starts, and the load balancer does not verify it, the pattern the Temporal UI uses. Governs R5. `coordination-service` and `repo-intelligence` gain optional TLS in their `ThreadingHTTPServer`. Langfuse web gets a TLS proxy sidecar. The collector's OTLP receivers read a certificate an init container issues into the task volume. No task key leaves the task.
- KTD4. The ALB certificate comes from ACM, issued or imported, with a name per service. The certificate task issues one from a generated CA or checks one the organization's PKI supplies. Governs R5.
- KTD5. Clients trust the platform CA through `SSL_CERT_FILE` pointing at a bundle of the system roots and the platform CA, which httpx 0.28 honors. Governs R5. No client code changes, and calls to GitHub and Atlassian still verify against the system roots.
- KTD6. Upstream images are mirrored to ECR by digest, because DNS Firewall resolves only AWS, GitHub, Atlassian, and Slack names. Governs R1.
- KTD7. `inv platform-deploy` orders db-migrate, the service rollout, schedule-bootstrap, and the drained-version prune, as `inv temporal-deploy` orders the Temporal schema and server. Governs R8, R6. Terraform registers the task definitions and leaves each service's task definition to the deploy step.
- KTD8. The collector exporters to X-Ray and Amazon Managed Service for Prometheus sit behind a toggle, and the debug exporter runs when it is off. Governs R10.

### High-Level Design

`infra/terraform/container-services` takes the cluster, the tiers, the Temporal client outputs, the Aurora, ElastiCache, and EFS endpoints, the secret ARNs, and the image references, and creates the per-service roles, groups, log groups, task definitions, and services, the EFS access points for the checkouts and the repository index, the shared internal ALB with its private hosted zone, and the one-shot task definitions. `private-bedrock` wires it behind `create_container_services`, adds its execution roles to `secret_readers`, and adds its services to `secret_restart_services`.

### Build order

1. ADP-154 creates the repositories and publishes the images.
2. ADP-155 adds the server TLS, the TLS sidecar and init image, the certificate task extension, and the shared ALB with its zone.
3. ADP-156 adds the module, the workers and services, the one-shot tasks, and `inv platform-deploy`.
4. ADP-157 adds Langfuse and ClickHouse, and ADP-158 adds the collector. Both depend on ADP-154 and ADP-155 and can land in either order.
5. ADP-159 adds the off-hours schedule and the rotation restarts.
6. ADP-136 wires `private-bedrock`, the README, and the compliance documents, and runs the end-to-end verification.

## Implementation Units

### U1. Images (ADP-154)

Create KMS-encrypted ECR repositories with immutable tags and scan on push for the platform image, the repository-intelligence image, the collector image, the TLS sidecar and init image, and mirrors of the pinned Langfuse web, Langfuse worker, and ClickHouse images. Add the RDS global CA bundle, pinned by SHA-256, to `docker/Dockerfile`. Add `inv publish-platform-images`, which builds the platform image with `AGENT_BUILD_ID`, builds the other images, and mirrors the upstream images by digest, each tagged with its version and a content hash.

### U2. Internal TLS and the shared load balancer (ADP-155)

Add optional TLS to `coordination-service` and `repo-intelligence`, enabled by a setting and served with a certificate issued at start. Add a small TLS image that either proxies a local port over TLS or issues a certificate into a task volume and exits. Extend the certificate task with an `--alb-name` list that issues the ALB certificate from the generated CA. Add the internal ALB, its listener and host rules, one HTTPS target group per service, the private hosted zone and records, and the DNS Firewall allowance.

### U3. Workers, services, one-shot tasks, and the deploy step (ADP-156)

Add the module with the five services, their roles and groups, the EFS access points, the db-migrate and schedule-bootstrap task definitions, and `inv platform-deploy`. Size the pools through settings, set `sslmode=verify-full` with the RDS CA path, and set the `TEMPORAL_*`, `SSL_CERT_FILE`, service URL, FIPS, and connectivity settings on every task.

### U4. Langfuse and ClickHouse (ADP-157)

Add ClickHouse on the `clickhouse-storage` access point as one task, and Langfuse web and worker against the `langfuse` database role, ElastiCache over TLS with its RBAC user, and the blob bucket through the task role. Route `langfuse-web` through the shared ALB and admit only the management tier and the jumphost task group.

### U5. Collector (ADP-158)

Run the collector with an AWS configuration that exports traces to X-Ray and metrics through SigV4 remote write to a Prometheus workspace the module creates, behind a toggle. Route OTLP gRPC through the shared ALB with a gRPC target group.

### U6. Off-hours schedule and rotation restarts (ADP-159)

Add EventBridge Scheduler schedules that set the database-holding services to zero and restore them, and add every service to `secret_restart_services` for the credentials it reads.

### U7. Composition root, documents, and verification (ADP-136)

Wire the module into `private-bedrock`, extend README Deploying to AWS, update the local-to-AWS mapping, the FedRAMP hardening design, and the control inheritance matrix, and record the sandbox verification below.

### EKS (ADP-137)

The EKS units are planned when ADP-137 starts, after ADP-136, ADP-139, and ADP-140.

## Verification Record

The units are recorded here as they complete.
