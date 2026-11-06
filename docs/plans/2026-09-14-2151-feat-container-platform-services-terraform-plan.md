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
- KTD8. The collector exporters to X-Ray and Amazon Managed Service for Prometheus sit behind a toggle, and the debug exporter runs when it is off. Governs R10. With Langfuse, the collector also forwards the spans with a `gen_ai.` attribute to Langfuse, and the review worker stops exporting to Langfuse itself, so each generation arrives once. X-Ray encryption is an account-wide regional setting, so the module changes it only on request.
- KTD9. Each Temporal worker runs as a blue and a green ECS service. Governs R8. The workers use PINNED versioning, so an execution runs only on workers of the build that started it, and a rolling update of one service would stop those workers before the execution finishes. The deploy starts a new build in the idle slot and scales the old slot to zero once a one-shot task reports that no running execution is pinned to its build.
- KTD10. Per-environment config files reach the tasks as the JSON environment value `PLATFORM_ENV_CONFIG`, which the image entrypoint writes under `PLATFORM_ENV_CONFIG_DIR`. The files hold settings and no credential, the root file system is read-only, and the task definition records which config a revision ran with.
- KTD11. ClickHouse serves only TLS, with a certificate the internal load balancer CA issues and a Secrets Manager secret holds, under a Cloud Map name. Governs R5. Langfuse reaches ClickHouse over HTTPS and runs its migrations over the native protocol, which the ALB cannot carry, and a client verifying a per-task certificate has no CA to trust. The deploy runs the ClickHouse migrations with verification, because the Langfuse entrypoint turns verification off for them.
- KTD12. A scheduled database or cache rotation falls inside the off-hours window, when ADP-159 has scaled the services that hold the credential to zero, so no running task opens a connection with the replaced password. Governs R7. The secret store rotates a single user, so a rotation while a service runs refuses its new connections until the restart rule replaces its tasks.

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

Add the module with the five services, the worker slots, their roles and groups, the EFS access points, the db-migrate and schedule-bootstrap task definitions, and `inv platform-deploy`. Size the pools through settings, set `sslmode=verify-full` with the RDS CA path, and set the `TEMPORAL_*`, `SSL_CERT_FILE`, service URL, FIPS, and connectivity settings on every task.

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

### U1 (ADP-154), 2026-11-05

- The apply created the six repositories, each with immutable tags, scan on push, and KMS encryption.
- `inv publish-platform-images` built the platform image as `0.3.0.20261105210216-4e99adf5d34c-arm64`, built the repository-intelligence and collector images, and copied Langfuse web 4.59.0, Langfuse worker 4.59.0, and ClickHouse 26.9.19.9. Each copy in ECR carries the index digest `compose.yaml` pins.
- A second run reported every image as pushed and built nothing, and the platform image kept its build ID. After a change to `uv.lock`, the next run minted build ID `0.3.0.20261105213111`, rebuilt the platform and repository-intelligence images, and reused the collector image.
- The platform image runs as `appuser` with `AGENT_BUILD_ID` set to its build ID, which the settings report, carries the RDS CA bundle with the pinned SHA-256 at mode 0444, and holds none of the local binaries under `vendor/bin`. A build with a wrong `RDS_CA_BUNDLE_SHA256` failed with `digest mismatch`, and the local compose build still carries the bundle.
- `put-image` of another manifest under an existing tag was refused with `ImageTagAlreadyExistsException`.
- Basic scanning completed for the platform and repository-intelligence images and for the arm64 image of each copy. It reports `UnsupportedImageError` for the distroless collector image. The repository-intelligence image reports four critical findings in its Debian packages, in `php8.4`, `brotli`, and `glibc`.

### U2 (ADP-155), 2026-11-05

- The plan first stopped at the module's input check, because the sandbox had one application subnet and an ALB needs two Availability Zones. A second application subnet, 10.0.2.0/24, was added, which also replaced the NAT instance and added the subnet to the endpoints and to the Temporal NLB and service. The apply then created the internal ALB, its listener and four host rules, four HTTPS target groups, the client, load balancer, and target security groups, the `platform.internal` private zone with four records, the log bucket, and the `tls-sidecar` repository. A full plan afterwards reported no changes.
- The ALB certificate was issued with `inv temporal-certs generate --internal-name` for the four names and imported into ACM.
- Temporary tasks ran repo-intelligence with `SERVER_TLS=true`, the TLS sidecar in front of a plain HTTP upstream on the Langfuse port, and the collector with its certificate issued by a `tls-sidecar issue` init container and an OTLP gRPC receiver over TLS. Each task registered in its target group with only its target security group admitting traffic, and all three reported healthy over the HTTPS health checks, which proves the hop from the ALB to each task is TLS.
- A client task in the client security group with `PLATFORM_CA_PEM` set reached repo-intelligence and the sidecar under their private names over TLS, got 503 from coordination, which had no target, got 404 for an unknown host name, and reached `github.com` through the NAT with the same bundle. The sidecar's upstream saw the original host and `X-Forwarded-Proto: https`. Without the platform CA the client failed with `CERTIFICATE_VERIFY_FAILED`. A TLS 1.1 handshake to the ALB was refused with `protocol version`.
- A span exported over OTLP gRPC to `otel-collector.platform.internal:443` with the bundle returned `SUCCESS` and appeared in the collector log, and the same export trusting only the system roots failed.
- The rebuilt local coordination-service and repo-intelligence containers stayed healthy over plain HTTP with the TLS settings unset.
- The temporary tasks, task definitions, execution role, log group, and security group were removed afterwards.

### U3 (ADP-156), 2026-11-06

- The apply created the five services, with the workflow and review workers in blue and green slots, the two one-shot task definitions, the per-workload roles, security groups, and log groups, the services KMS key, the EFS workspace with its two access points, and the `bedrock-runtime-fips` interface endpoint. A full plan afterwards reported no changes.
- `inv platform-deploy` ran `db-migrate` as `platform_migrator` to exit 0, moved coordination, repo-intelligence, and the index worker to their task definitions, started both workers in their blue slots, and ran `schedule-bootstrap` and the prune to exit 0. Every service reached a steady state with passing health checks, coordination and repo-intelligence over HTTPS behind the internal load balancer.
- The workers passed the schema check against Aurora with `PGSSLMODE=verify-full`, connected to Temporal over TLS, and registered build `0.3.0.20261105235720` as the current version of the Worker Deployment. The review worker passed the review signing preflight with the injected key and the trust root from `PLATFORM_ENV_CONFIG`.
- A CountingWorkflow started on build A, `0.3.0.20261105235720`, and was pinned to it. After build B, `0.3.0.20261106001649`, was published and applied, the deploy started B in the green slots, the pinned-execution task reported the execution on A, and both blue slots kept running. A new execution started on B. A temporary activity worker on build A then let the first execution complete on A with result 3. The next deploy found nothing pinned to A and scaled both blue slots to zero.
- A temporary task with write access seeded a git checkout on the checkouts access point. The review worker's task definition, with a command override, read the checkout and its history from `/repositories`, was refused a write as a read-only file system, and signed a verdict with `SshsigAttestor` that verified, while a tampered payload failed verification.
- The one-shot commands exited 133 on SIGTRAP during Python finalization after they had succeeded, the Temporal Python SDK issue temporalio/sdk-python#300. They now exit without finalization, and the prune exits 0.
- The temporary task definitions, the seed role, and the seeded checkout were removed afterwards.

### U4 (ADP-157), 2026-11-06

- The apply created ClickHouse, Langfuse web, Langfuse worker, and the ClickHouse migration task with their roles, groups, and log groups, the `data.platform.internal` Cloud Map namespace, the ClickHouse TLS secret, the `langfuse_admin_password` secret, the jumphost rules at the internal load balancer, and the restart rules for the Langfuse secrets. A full plan afterwards reported no changes.
- The first apply failed on the volume name `config.d`, which ECS refuses, and the volumes were renamed. The first deploy stopped Langfuse with `P1011 certificate verify failed`, because Prisma reads only the first certificate of an `sslcert` file and the RDS bundle starts with another region's root. Langfuse now verifies Aurora through `SSL_CERT_FILE`, a bundle the setup container writes.
- `inv clickhouse-certs issue` and `put` stored a certificate for `clickhouse.data.platform.internal` and 127.0.0.1. `inv platform-deploy` started ClickHouse, which passed its health check over verified TLS on loopback, ran `clickhouse-migrate` to exit 0 with verification against the platform CA, and moved Langfuse web and worker, which reached a steady state.
- A task on the review worker's task definition recorded a generation span. It reached Langfuse over TLS through the internal load balancer, passed through the bucket, the cache, and the worker, and `/api/public/v2/observations` returned it from ClickHouse. The Langfuse v4 image serves observations from the v4 events tables, and answers 404 on the v1 `/api/public/traces` and `/api/public/observations` endpoints.
- A forced ClickHouse deployment replaced task `40d03547` with `4092455f`, and the observation was still returned afterwards.
- `inv langfuse-ui` forwarded `localhost:8444` through the jumphost. Langfuse answered under the platform CA and failed without it, the administrator signed in with the generated password once the operator user was listed as its reader, the session listed the `agentic-dev-platform` project, and sign-up answered `Sign up is disabled.` A task in the application subnets outside the load balancer client group timed out on Langfuse web ports 3000 and 3443, on ClickHouse ports 8443 and 9440, and on the load balancer.
- A rotation of the `langfuse` database password while both services ran restarted them 12 seconds after it finished, and the old worker task logged 32 refused connections over 32 seconds until its replacement took over. A second rotation with both services scaled to zero, as the ADP-159 off-hours schedule leaves them during the rotation window, redeployed them at zero tasks, and both started on the new password with no failed connection.
- The tasks that ran without a collector exported logs to `localhost:4317`. `OTEL_LOGS_EXPORTER=none` now skips that exporter, as the traces and metrics settings do.
- Scheduled reap and intake runs wait on activities that only the EKS activity worker serves, so they stay pinned to their build and kept an older worker slot running. The deploy now reuses a slot whose build nothing is pinned to. The stranded runs were terminated, and the schedules keep starting runs that strand until ADP-137 adds the activity worker.
- The probe security group and the jumphost task were removed afterwards.

### U5 (ADP-158), 2026-11-06

- The apply created the collector service with its roles, group, and log group, the telemetry KMS key, the Prometheus workspace, the `xray-fips` and `aps-workspaces` interface endpoints, and the regional X-Ray encryption configuration on the telemetry key, and added the internal load balancer client group to the platform services. `inv platform-deploy` moved the collector and every platform service, and the collector reached a steady state behind the load balancer. A full plan afterwards reported no changes.
- The rendered configuration passed `otelcol-contrib validate` from the pinned image with the exports and Langfuse forward on, with the exports alone, and with the toggle off.
- A task on the review worker's task definition exported to `https://otel-collector.platform.internal:443`. Its trace `1-4b1081bc-afcbad683a31e2c9be2bce4b` was returned by `aws xray batch-get-traces` while X-Ray encryption used the telemetry key, and `adevx_gate_outcomes_total{gate="adp158-probe"}` read 3 from the workspace. ClickHouse held the generation span and not its parent, so the collector forwarded only the generation to Langfuse.
- The collector's groups admit egress only to the endpoints group, the S3 prefix list, and the internal load balancer. Both endpoints use private DNS, and a task resolved `xray-fips.us-east-1.amazonaws.com` and `aps-workspaces.us-east-1.amazonaws.com` to the endpoint addresses in the two application subnets.
- A one-off collector task with the exports-off configuration reported healthy and started only the OTLP receiver, the health check extension, and the debug exporter.
- With the collector on, the review worker's execution role first stopped reading the Langfuse keys, and a new task of the older revision in the draining blue slot failed to start on the secret policy. The review worker now reads the keys whenever Langfuse runs, and only `LANGFUSE_HOST` starts its own export. A new task of the blue revision then started.

### U6 (ADP-159), 2026-11-06

- The apply created the `off-hours` sleep and wake schedules in America/Chicago, their Automation documents, roles, and count parameter, and secret store restart rules that list every service and worker slot under each credential it reads, five under `database_platform_app`. A full plan afterwards reported no changes.
- A one-off EventBridge Scheduler schedule with the sleep schedule's target and role started the sleep document. It recorded the 13 running services, stopped the platform services, then the Temporal server and UI, ClickHouse, and the collector.
- A rotation of the `platform_app` password with every service at zero redeployed the five services that read it within two seconds, and none started a task. The Aurora cluster paused at 0 ACU at 19:59 UTC, an hour after the rotation's last connection.
- A one-off schedule started the wake document. The foundation services ran within 95 seconds, the other services started, every desired count matched the count before the sleep, and the parameter was empty again.
- The first sleep reported every service stopped after 40 seconds, while a coordination task that was still stopping opened a connection during the rotation that followed and logged three refused logins. ECS lowers the running count as soon as a task starts to stop, so the sleep now waits until the stopping tasks reach `DEPROVISIONING` or `STOPPED`. On the next sleep the stop step waited two minutes and 29 seconds, no service logged a line after it ended, a rotation right after it logged no refused login, and the wake restored every count.
- The reap and intake schedules now catch up at most one missed run when Temporal returns, because the default one-year catch-up window would start one run per missed interval.
