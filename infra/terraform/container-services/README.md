# container-services

Module that runs the platform containers on the platform ECS cluster. Nothing is created unless `create_container_services = true`.

## What it provisions

- ECR repositories `<name_prefix>/platform`, `<name_prefix>/repo-intelligence`, `<name_prefix>/otel-collector`, `<name_prefix>/langfuse-web`, `<name_prefix>/langfuse-worker`, and `<name_prefix>/clickhouse` through the [ecr](../ecr) module, each with immutable tags, scan on push, KMS encryption, and a lifecycle policy that expires untagged images. `kms_key_arn` sets one key for all of them, and empty creates one key per repository. The `tls-sidecar` repository holds the TLS image described below. The `opa`, `litellm`, `openhands-agent-server`, `buildah`, `semgrep`, and `trivy` repositories hold the images the EKS pods run, the OPA sidecar of the activity worker, LiteLLM, the sandbox agent-server, and the builder and scanner images of the gate, checks, and scanner pods. The `gate-engine-security-policy` and `gate-engine-test-policy` repositories hold the [example gate engine](../../../examples/gate-engines) images the gate pods run in image mode, `agent-server-jvm` and `agent-server-go` the catalog sandbox images, and `checks-python`, `checks-node`, `checks-jvm`, and `checks-go` the catalog checks images of the [runtime profiles](../../../docs/runtime-profiles.md). `al2023-base` and `al2023-openscap` hold the hardened base and the CIS scanner that the parallel jobs of the `publish-images` workflow share, which no workload runs.
- With `internal_acm_certificate_arn` set, the internal load balancer described below, its private hosted zone, and the `<name_prefix>-internal-lb-logs-<account>` bucket, which receives its access logs under `internal/`. Load balancer log delivery requires S3-managed encryption, so the bucket uses SSE-S3, denies requests without TLS, expires logs after `load_balancer_log_retention_days`, and sends its own server access logs to `access_log_bucket`.
- With `platform_image`, `repo_intelligence_image`, and `temporal` set as well, the platform services, the one-shot tasks, their task and execution roles, security groups, and log groups, a KMS key for the logs and the workspace, and the workspace file system, all described below.
- With `collector_image` set as well, the collector service, and with `collector_exports_enabled`, the Prometheus workspace and the telemetry KMS key, described under Collector.
- With `langfuse` set as well, ClickHouse, Langfuse web, Langfuse worker, and the ClickHouse migration task, the Cloud Map namespace that names ClickHouse, and the ClickHouse TLS secret, described under Langfuse and ClickHouse.

## Images

The ECS tasks and the EKS pods pull only from these repositories, because DNS Firewall resolves no public registry. `inv publish-platform-images` builds or copies every image for `linux/arm64`, or `--arch amd64`, prints each reference with its digest, and writes the references to `images.auto.tfvars.json` in the composition root.

| Repository | Source | Tag |
|---|---|---|
| `platform` | `docker/Dockerfile`, built with `AGENT_BUILD_ID` | `<build ID>-<source hash>-<arch>` |
| `repo-intelligence` | `docker/repo-intelligence.Dockerfile` | `<release version>-<content hash>-<arch>` |
| `otel-collector` | `docker/otel-collector.Dockerfile` | `<collector version>-<content hash>-<arch>` |
| `tls-sidecar` | `docker/tls-sidecar.Dockerfile` | `<release version>-<content hash>-<arch>` |
| `langfuse-web`, `langfuse-worker`, `clickhouse` | The image `compose.yaml` pins for that service, copied by digest | The upstream version |

The platform image runs every worker role and the coordination service. Its build ID is the Temporal Worker Deployment version the workers register, the release version followed by a UTC timestamp, which `scripts/build_id.py` mints so that a newer build orders after an older one. The source hash covers the files `scripts/build_id.py` digests and the architecture, so a publish of sources whose image is already in the repository reuses that image and its build ID, and a change mints a new build ID.

The platform image carries the RDS global CA bundle at `/etc/ssl/rds-global-bundle.pem`. The build fetches it from `RDS_CA_BUNDLE_URL` and fails unless its SHA-256 matches `RDS_CA_BUNDLE_SHA256`, the same checksum the Temporal images pin.

The built images come from a copy of the files git tracks or would track, so ignored local artifacts, such as the binaries under `vendor/bin`, never reach an image. A copied image keeps the multi-architecture digest its source pins, which is `compose.yaml` for Langfuse and ClickHouse, `deploy/kubernetes/litellm.yaml` for LiteLLM, and the default of the `openhands_agent_server_image`, `gate_engine_builder_image`, `semgrep_image`, and `trivy_image` settings for the others. The publish fails when the repository holds a different digest under that tag, and the error names the source. The OPA image is built from `docker/opa.Dockerfile` and tagged with the OPA version its base pins. The repository lifecycle policy does not expire the per-architecture images an index references.

Basic scanning scans the per-architecture images of a copied index, not the index itself. It cannot scan the collector image, whose distroless base has no package database, and reports `UnsupportedImageError`. Amazon Inspector enhanced scanning, an account-level setting, scans the Go binaries in it.

## Internal load balancer

The platform services that other workloads call sit behind one internal ALB in the application tier, which needs `application_subnet_ids` in two Availability Zones. Its HTTPS listener presents `internal_acm_certificate_arn` under `ELBSecurityPolicy-TLS13-1-2-Res-FIPS-2023-04`, which negotiates TLS 1.3 and TLS 1.2 with AES-GCM suites only, and routes each name in the private hosted zone `internal_domain` to its service. A name it does not know answers 404.

| Name | Target port | Target protocol |
|---|---|---|
| `coordination.<internal_domain>` | 8130 | HTTPS |
| `repo-intelligence.<internal_domain>` | 8120 | HTTPS |
| `otel-collector.<internal_domain>` | 4317 | HTTPS with the gRPC protocol version |
| `langfuse.<internal_domain>` | 3443 | HTTPS |

The load balancer re-encrypts to every task, and each task serves TLS with a certificate it issues when it starts, so no task key leaves the task. The load balancer does not verify that certificate.

- `coordination-service` and `repo-intelligence` serve HTTPS when `SERVER_TLS=true`, with a P-256 certificate they write under `SERVER_TLS_DIR`. `python -m services.healthcheck http PORT --tls` probes them and trusts only that certificate.
- Langfuse web serves plain HTTP, so the `tls-sidecar` image fronts it with `tls-sidecar proxy -listen :3443 -upstream http://127.0.0.1:3000`, which forwards the original `Host` and sets `X-Forwarded-Proto: https`.
- The collector reads its certificate from files, so an init container runs `tls-sidecar issue -out DIR -owner 10001:10001` as root into a task volume, and the collector's OTLP receivers serve TLS with `cert.pem` and `key.pem` from it.

The ALB admits port 443 from the `client_security_group_id` group, which callers attach, from `internal_allowed_cidr_blocks`, and from each group in `internal_operator_security_group_ids`, such as the jumphost task group, which gains the matching egress rule. Each service's target group admits only its port from the ALB, and its tasks attach the matching `target_security_group_ids` group.

Clients verify the ALB certificate against the CA that issued it. The platform image's entrypoint reads that CA from `PLATFORM_CA_PEM`, writes it with the system roots to one bundle, and points `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, and `OTEL_EXPORTER_OTLP_CERTIFICATE` at it, so httpx, requests, and the gRPC OTLP exporter verify both the internal names and public hosts such as GitHub. A CA certificate is public, so it travels as a plain environment value.

The private zone names fall under `*.internal`, which DNS Firewall already resolves. The `private-bedrock` root adds any other `internal_domain` to the allowlist.

### Certificate

ACM cannot issue a public certificate for a private name, so the ALB certificate is imported. Issue it from the generated CA with `inv temporal-certs generate --out DIR --internal-name coordination.platform.internal,repo-intelligence.platform.internal,otel-collector.platform.internal,langfuse.platform.internal`, or have the organization's PKI issue a server certificate with those names, and import it with `aws acm import-certificate --certificate fileb://DIR/internal.pem --private-key fileb://DIR/internal-key.pem --certificate-chain fileb://DIR/ca.pem`. The CA PEM becomes `PLATFORM_CA_PEM` for the clients.

## Platform services

Every service runs on Fargate in the application subnets without a public address, and starts at zero tasks. The module runs no review worker. Each tenant's review worker runs on EKS in `tenant-<tenant>-review` under the tenant's own role, which the [tenant](../tenant) module grants and the `private-bedrock` root binds. The one-shot tasks run in the management subnets. `inv platform-deploy` sets each count from `service_counts`.

| Service | Image | Command | Reaches |
|---|---|---|---|
| `workflow-worker` | platform | `services.orchestration.worker` with `WORKER_ROLE=workflow` | Database, Temporal |
| `coordination` | platform | `services.coordination.service` over HTTPS on 8130 | Database, Temporal, behind the internal load balancer |
| `repo-intelligence` | repo-intelligence | `services.repository_intelligence.server` over HTTPS on 8120 | The index, behind the internal load balancer, and the checkouts read-only, or with `tenant_workspaces` every tenant's workspace read-only at `/tenants`, the `tenants/` prefix of the tenant config bucket, and each tenant's repository intelligence token through its task role |
| `repo-index-worker` | repo-intelligence | `services.repository_intelligence.indexing.worker` | The index, and the checkouts read-only, or with `tenant_workspaces` every tenant's workspace read-only at `/tenants` and the `tenants/` prefix of the tenant config bucket |
| `db-migrate` | platform | `alembic upgrade head`, once per deploy | Database as `platform_migrator` |
| `schedule-bootstrap` | platform | `scripts.schedules`, once per deploy, which removes the untenanted intake and reap schedules that each tenant's worker replaces | Database, Temporal |

Each workload has its own task role, execution role, and security group. The security group has no ingress and admits egress to the VPC endpoints, S3, and only the database, workspace, Temporal, or load balancer the workload reaches. Each execution role reads only the secrets its task injects. The task definitions carry secret ARNs, never values. A workload that reaches the database receives `PGUSER` and `PGPASSWORD` from the `platform_app` secret, or the `platform_migrator` secret for `db-migrate`, and connects with `PGSSLMODE=verify-full` against the RDS CA bundle in the image. A workload that reaches Temporal receives the Temporal CA as `TEMPORAL_TLS_CA` and attaches the Temporal client group. The root file systems are read-only, and a task volume mounted at `/tmp` holds what a task writes.

`use_fips_endpoints` sets `AWS_USE_FIPS_ENDPOINT=true`, which sends every boto3 call to the `-fips` endpoint of its service, `bedrock-runtime-fips` included. `https_proxy` sets `HTTPS_PROXY` and a `NO_PROXY` that keeps the internal domain, AWS, and the task metadata endpoint off the proxy. With the collector, every platform task exports over OTLP to `https://otel-collector.<internal_domain>:443` and joins the internal load balancer client group. Without it, `OTEL_TRACES_EXPORTER`, `OTEL_METRICS_EXPORTER`, and `OTEL_LOGS_EXPORTER` are `none`, and `OTEL_SDK_DISABLED=true` stops span recording unless Langfuse runs. `LITELLM_LOCAL_MODEL_COST_MAP=True` keeps LiteLLM on the cost map in its package in place of fetching one from GitHub at import.

### Temporal worker slots

A Temporal worker runs as two services, `<name>-blue` and `<name>-green`. The workers register their build as a version of the Worker Deployment with PINNED versioning, so a running execution stays on the build that started it. `inv platform-deploy` starts a new build in the idle slot, where the workflow workers make it the current version, so new executions start on it while the old slot serves the executions pinned to the old build. The deploy runs `python -m scripts.pinned_executions <build>` as a one-shot task and scales the old slot to zero when it exits 0, which means no running execution is pinned to that build. Exit 3 lists the pinned executions and leaves the slot running, and a later deploy scales it to zero once they finish. A deploy whose build already runs in a slot replaces that slot's tasks in place. A deploy of a third build while both slots still run older builds stops before it moves any service.

### Configuration

`environment_config` maps a file name to its content, the platform's config files such as `integrations.yaml`, `review-trust-root.yaml`, and `tickets_isolation.yaml`, and reaches every platform and repository-intelligence task as the JSON value of `PLATFORM_ENV_CONFIG`. The platform image's entrypoint writes each `.yaml` or `.yml` file under `${TMPDIR}/platform-config` and exports `PLATFORM_ENV_CONFIG_DIR` there, which the services read in place of `config/local`. The `private-bedrock` root fills it from `container_config_dir` through the [platform-config](../platform-config) module, which refuses a tenant-only file such as `github.yaml`, because each tenant's files come from its config bundle. The `config/platform` files are in the image. `service_environment` adds or overrides environment values per workload.

### Database connections

Each SQLAlchemy engine holds its own pool of `DATABASE_POOL_SIZE` connections and opens up to `DATABASE_POOL_MAX_OVERFLOW` more under load, set from `database_pool_size` and `database_pool_max_overflow`. A process opens one engine per store it uses, and coordination opens six, so the peak per task is about the engine count times the sum of the two. Size the Aurora maximum connections for the sum over every running task, both worker slots included.

### Workspace

An encrypted EFS file system with elastic throughput holds the repository checkouts and the repository index, through the access points `checkouts` at `/checkouts` and `index` at `/repository-indexes`, both owned by uid 10001, the `appuser` of both images. Its policy admits only TLS connections through an access point. Its mount targets admit NFS from the tasks that mount it and from each group in `workspace_client_security_group_ids`, such as the EKS cluster security group. Repository intelligence and the index worker mount `checkouts` read-only at `/repositories` when `tenant_workspaces` is empty. Repository intelligence and the index worker mount `index` read-write at `/app/repository_indexes`. Every platform and repository-intelligence task sets `REPOSITORY_ROOT` to `/repositories`, so without tenants each repository in the environment's `repositories.yaml` is read at `/repositories/<name>`, and repository intelligence indexes those with `index: true`. `environment_config` carries `repositories.yaml` into the tasks with the other config files.

Each name in `tenant_workspaces` receives an access point rooted at `/tenants/<tenant>`, which the tenant's activity worker mounts read-write at the same path from EKS. With any tenant the module adds the `tenants` access point at `/tenants`, which repository intelligence and the index worker mount read-only at `/tenants`, so they read each tenant's repositories under `/tenants/<tenant>/repositories`. Repository intelligence and the index worker then no longer mount `checkouts`. Each tenant's worker writes its checkouts through its own access point, and no workload of this module writes `checkouts`. The platform environment and the repository intelligence environment carry the tenant names as `TENANTS`, which the workflow worker reads to poll each tenant's task queue.

`tenant_config_bucket_arn` names the tenant config bucket, whose name reaches repository intelligence and the index worker as `TENANT_CONFIG_BUCKET`, with `AWS_REGION` and `AWS_USE_FIPS_ENDPOINT`. Their task roles list `tenants/` of the bucket with `s3:ListBucket` and `s3:ListBucketVersions`, read `tenants/*` with `s3:GetObject` and `s3:GetObjectVersion`, and decrypt with `tenant_config_kms_key_arn` through S3 only. Empty values grant nothing. `repo_intelligence_tokens` maps each tenant to its repository intelligence token secret and the tenant key that encrypts it. ECS injects no token into a task. Repository intelligence receives the secret ARNs as `REPO_INTELLIGENCE_TOKEN_SECRET_ARNS`, a JSON map of tenant to secret ARN, and its task role reads those secrets with `secretsmanager:GetSecretValue` and decrypts with those keys through Secrets Manager only, so the service reads each tenant's `AWSCURRENT` and `AWSPREVIOUS` token at runtime and a rotation refuses no worker that still holds the previous token. No execution role reads the tokens. The tokens are secrets of the [tenant](../tenant) module, outside the secret store, so `secret_readers` and `secret_restart_services` name none of them. The tenants of `repo_intelligence_tokens` must match `tenant_workspaces`.

## Langfuse and ClickHouse

`langfuse` takes the Langfuse web, Langfuse worker, and ClickHouse images, the URL operators open, the administrator email, the ElastiCache endpoint and group, the `clickhouse-storage` file system and access point, and the `langfuse-blob-store` bucket. `tls_sidecar_image` fronts Langfuse web. `langfuse_cache_secret_arn` names the cache user secret, a separate input because the secret store key that encrypts it lists the roles this module creates. The tasks run the upstream images with read-only root file systems. A `setup` container from the platform image runs first as root and prepares the task volumes those images read.

| Service | Reaches | Secrets |
|---|---|---|
| `clickhouse` | Its file system over NFS with TLS through the access point, as uid 101 | `clickhouse_password`, and the certificate and key in the module's ClickHouse TLS secret, which only its execution role reads |
| `langfuse-web` | Aurora as the `langfuse` role, the cache, ClickHouse, the bucket, behind the internal load balancer through the TLS sidecar | `database_langfuse`, the cache user, `clickhouse_password`, `langfuse_salt`, `langfuse_encryption_key`, `langfuse_nextauth_secret`, `langfuse_public_key`, `langfuse_secret_key`, `langfuse_admin_password` |
| `langfuse-worker` | Aurora as the `langfuse` role, the cache, ClickHouse, the bucket | `database_langfuse`, the cache user, `clickhouse_password`, `langfuse_salt`, `langfuse_encryption_key` |
| `clickhouse-migrate` | ClickHouse, once per deploy | `clickhouse_password` |

ClickHouse serves only HTTPS on 8443 and the native protocol over TLS on 9440, and its group admits those ports from the three Langfuse groups only. Cloud Map registers the task as `clickhouse.<clickhouse_namespace>`, which defaults to `clickhouse.data.<internal_domain>`. A deployment stops the running task before it starts the next, so two servers never open the same data directory, and the data survives a task replacement on the file system. The certificate comes from the CA the clients already trust. Issue it with `inv clickhouse-certs issue --dir DIR`, where `DIR` holds the `ca.pem` and `ca-key.pem` that `inv temporal-certs generate --internal-name` wrote, and store it with `inv clickhouse-certs put --dir DIR`, which checks the CA, the key, the name, and the expiry. It names the Cloud Map name and 127.0.0.1, so the task's health check verifies it on loopback.

Langfuse verifies every connection. The setup container writes the platform CA and a bundle of the system roots, the RDS roots, and the platform CA to a task volume. Node trusts the platform CA through `NODE_EXTRA_CA_CERTS` for ClickHouse, ElastiCache presents an Amazon certificate Node already trusts, and Prisma verifies Aurora with `sslaccept=strict` through `SSL_CERT_FILE`, because Prisma reads only the first certificate of an `sslcert` file. The Langfuse web entrypoint migrates ClickHouse with certificate verification off, so `LANGFUSE_AUTO_CLICKHOUSE_MIGRATION_DISABLED=true` stops it, and `inv platform-deploy` runs the same migrations from `clickhouse-migrate` with verification on before it moves Langfuse. Langfuse web still migrates its Postgres schema at start. The task role reads and writes the bucket objects, and with a bucket key, encrypts them.

Langfuse web creates the organization, the `agentic-dev-platform` project with the generated project keys, and the administrator from `admin_email` and `langfuse_admin_password` on its first start, and refuses sign-up. The administrator reads the password after the operator role or user is listed under `langfuse_admin_password` in the secret store readers. `NEXTAUTH_URL` is `public_url`, the URL `inv langfuse-ui` forwards through the jumphost. Langfuse's FIPS mode needs a Government license and hosts booted in FIPS mode, which Fargate in a commercial region does not offer, so Langfuse uses the image's OpenSSL without the FIPS provider.

## Collector

`collector_image` runs the collector as the OTLP gateway for the ECS and EKS workloads, behind the internal load balancer on `otel-collector.<internal_domain>`. An init container runs `tls-sidecar issue` from `tls_sidecar_image`, and the OTLP gRPC receiver serves TLS with that certificate on 4317. The task renders its configuration from `collector-config.yaml.tftpl` and reads it with `--config=env:COLLECTOR_CONFIG`. It applies the memory limiter, the attribute redaction, and the batch processor of `observability/otel/otel-collector-config.yaml`, the configuration the local stack runs.

With `collector_exports_enabled`, the default, the collector sends:

- traces to X-Ray through the `awsxray` exporter, at the `xray-fips` endpoint when `use_fips_endpoints` is true
- metrics to a Prometheus workspace the module creates, through Prometheus remote write signed with SigV4
- with Langfuse, the spans that carry a `gen_ai.` attribute to Langfuse over OTLP HTTP through the internal load balancer, with basic auth from the project keys.

The task role grants only `xray:PutTraceSegments`, `xray:PutTelemetryRecords`, and `aps:RemoteWrite` on the workspace. The collector's group admits egress only to the VPC endpoints, S3, and the internal load balancer, so X-Ray and Prometheus are reached only through their interface endpoints, which the `private-bedrock` root adds. AWS offers no FIPS endpoint for Prometheus remote write. Logs go to the debug exporter, which writes counts to the task log. With `collector_exports_enabled = false`, every pipeline ends in the debug exporter, and the module creates no workspace, telemetry key, or task permission.

The telemetry KMS key encrypts the workspace. X-Ray encryption is one setting per account and region, so the module leaves it alone unless `xray_encryption_enabled` sets it to the telemetry key, which also lets the collector use the key. The [managed-grafana](../managed-grafana) module provisions the dashboards over the workspace and the traces, with a query role that may only read the workspace.

## Outputs

| Output | Value |
|---|---|
| `repository_urls` | ECR repository of each image, keyed by the name `inv publish-platform-images` uses. Empty when disabled |
| `repository_arns` | ARN of each image's repository, keyed as `repository_urls`. Empty when disabled |
| `internal_endpoints` | The client security group, each service's target security group and target group, each service's HTTPS URL, and the domain. Null without the internal load balancer |
| `execution_role_arns` | Execution role of each workload |
| `secret_readers` | Execution roles that inject each secret, keyed by its secret store key, for the secret store |
| `service_arns` | ECS service of each service and worker slot |
| `workspace` | The workspace file system, its access points, and its security group, for the workloads outside the module that write the checkouts |
| `platform_environment` | The environment every platform task receives, without the config files, for the EKS workloads that run the platform image. Null without the services |
| `deploy` | The cluster, services, worker slots, Langfuse and ClickHouse services, one-shot tasks, and subnets `inv platform-deploy` reads |
| `temporal_client_role_arns` | Execution roles of the workloads that connect to Temporal, which read the Temporal CA |
| `repository_intelligence_roles` | The repository intelligence execution role, the repository intelligence task role as `token_reader_role_arn`, which reads the tenant tokens at runtime, and the task roles of repository intelligence and the index worker as `task_role_arns`, which read the tenant config bundles. Null without the services |
| `secret_restart_services` | The services and worker slots that read each secret, the `platform_app` login included, for the secret store's restart rules |
| `clickhouse` | The ClickHouse TLS secret and the name its certificate carries. Null without Langfuse |
| `langfuse` | The internal Langfuse URL, the public URL, and `direct_span_exports`, true when no collector runs, so a workload outside the module that traces its generations, such as each tenant's EKS review worker, exports them to Langfuse itself. Null without Langfuse |
| `telemetry` | The Prometheus workspace ID, ARN, and endpoint, the X-Ray encryption key the module set, and the collector URL. Null without collector exports |

## Tests

`tests/container_services.tftest.hcl`, `tests/internal_lb.tftest.hcl`, `tests/services.tftest.hcl`, `tests/langfuse.tftest.hcl`, and `tests/collector.tftest.hcl` run the module against a mocked provider. They assert that the disabled module creates nothing, that the enabled module creates a repository for every image, that the internal listener terminates TLS under the FIPS policy with access logs, that every target group re-encrypts and each host rule forwards to its own service, that each target admits only the load balancer, that the tasks inject secrets by ARN and each execution role reads only its own, that the module plans no review worker, no role of one, and no reader of the review signing key or the review attestation secret, that only the index writers write the index, that each service joins only the groups it needs, that each Temporal worker has two slots, that the workspace admits only TLS through an access point and NFS from the listed client groups, that with tenants repository intelligence and the index worker mount `/tenants` read-only in place of the checkouts, read only `tenants/` of the config bucket, that only repository intelligence receives the tenant token secret ARNs and only its task role reads the tokens, with no execution role injecting one, that without tenants repository intelligence reads the checkouts, receives no token map, and reads no token, that Langfuse and ClickHouse inject their secrets by ARN and JSON field with read-only root file systems, that only the ClickHouse execution role reads its key, that ClickHouse serves only TLS, admits only the Langfuse groups, and never runs two tasks, that Langfuse web sits behind the load balancer through its sidecar, that the secret store restarts the Langfuse services after a rotation, that every platform task exports to the collector through the load balancer, that the `langfuse` output names direct span exports only without a collector, that the collector exports to X-Ray over FIPS, to its workspace, and to Langfuse with only those permissions, that it exports nothing with the toggle off, that X-Ray encryption changes only on request, and the input checks. Run it with `inv terraform-test`, which CI also runs.
