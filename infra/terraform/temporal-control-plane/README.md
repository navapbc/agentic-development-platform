# temporal-control-plane

Module that provisions the OSS Temporal control plane on the platform ECS cluster. Nothing is created unless `create_temporal_control_plane = true`.

## What it provisions

- ECR repositories `<name_prefix>/temporal-server`, `<name_prefix>/temporal-admin-tools`, and `<name_prefix>/temporal-ui` through the [ecr](../ecr) module, each with immutable tags, scan on push, KMS encryption, and a 14-day expiry for untagged images. `kms_key_arn` applies one key to both, and empty creates one key per repository.
- A KMS key, `alias/<name_prefix>-temporal`, and the TLS secrets `<name_prefix>/temporal/tls-ca`, `<name_prefix>/temporal/tls-internode`, and, unless the frontend comes from ACM, `<name_prefix>/temporal/tls-frontend`. Each secret policy denies `GetSecretValue` to every principal outside `tls_secret_readers`.
- With `server_image` and `admin_tools_image` set, the server ECS service on `cluster_arn` in the application tier behind an internal NLB on port 7233, the admin-tools task definition for the schema and namespace actions, their execution roles, security groups, and KMS-encrypted log groups under `/aws/ecs/<name_prefix>-temporal/`.
- With `ui_image` and `ui_acm_certificate_arn` also set, the UI ECS service in the management tier behind an internal HTTPS ALB that admits `ui_allowed_cidr_blocks` and the members of `ui_operator_security_group_ids` only. The ALB needs two management subnets in different Availability Zones.
- With the server deployed, the `<name_prefix>-temporal-lb-logs-<account>` bucket, which receives the NLB access logs under `frontend/` and the ALB access logs under `ui/`. Load balancer log delivery requires S3-managed encryption, so the bucket uses SSE-S3, denies requests without TLS, expires logs after `load_balancer_log_retention_days`, and sends its own server access logs to `access_log_bucket`. The NLB writes access logs for TLS listeners only, so it writes none when the frontend certificate comes from secrets and the listener is TCP.
- With an ACM certificate to track, an EventBridge rule on its `ACM Certificate Approaching Expiration` and `ACM Certificate Expired` events and a KMS-encrypted SNS topic, `certificate_expiry_topic_arn`, for the operators to subscribe to.

## TLS certificates

The frontend presents a certificate to the workers and the UI, and the Temporal services authenticate each other over mutual TLS with the internode certificate. Each certificate comes from one of three sources.

| Source | Frontend | Internode |
|---|---|---|
| Generated | `inv temporal-certs generate --out DIR` issues a self-signed CA and both pairs | Same |
| Your PKI | Your PEM files in a directory, under the file names below | Same |
| ACM | Set `frontend_acm_certificate_arn`, and the NLB presents that certificate, issued by ACM or imported into it. No frontend secret is created | Import it with `inv temporal-certs put --import-to-acm`, or import it yourself, and set `internode_acm_certificate_arn` to track its expiry. The server still reads it from Secrets Manager |

ACM never returns the private key of an imported certificate, and the internode certificate must reach the server process, so ACM tracks the internode certificate and Secrets Manager serves it.

`inv temporal-certs put --dir DIR` writes a directory to the secrets. The directory holds `ca.pem`, `internode.pem`, `internode-key.pem`, and, unless the frontend comes from ACM, `frontend.pem` and `frontend-key.pem`. `ca.pem` may hold the whole chain. Before it writes anything, `put` checks that each certificate verifies against `ca.pem`, that each key belongs to its certificate, that the internode certificate carries both the server and client authentication usages, and that no certificate has expired. It refuses frontend files when the frontend comes from ACM, and it refuses an internode certificate that differs from the one `internode_acm_certificate_arn` tracks. `--import-to-acm` re-imports into that ARN, or imports a new certificate and prints the ARN to set.

`generate` refuses a directory inside the repository and refuses to replace existing files without `--force`. It writes P-256 keys and certificates with mode 0600. The frontend certificate names `frontend.temporal.internal`, and the internode certificate `internode.temporal.internal`, unless `--frontend-name` and `--internode-name` set others. The CA key stays in the directory so the operator can issue again. Keep it offline.

Certificate material never passes through Terraform, so a plan or apply needs no key and the Terraform state holds none. A rotation is a `put` followed by a redeploy of the server.

## Server

Each server task runs every Temporal service, so `server_count` tasks form one cluster through their membership ports. The task starts through `/etc/temporal/platform-start.sh`, which writes the injected TLS material and the dynamic configuration to files in a task volume, removes them from the environment, and execs the upstream entrypoint with `/etc/temporal/platform-config.yaml`. That file is the upstream configuration template, fetched at build time and checked against a pinned SHA-256, with the frontend listener reading its own client authentication setting. The internode listeners require a client certificate from the CA, and the frontend requires none. The root filesystem is read-only, and a container that runs before the main one opens the task volume to the image's unprivileged user.

The server connects to Aurora as the `temporal` role with TLS and host verification against the RDS CA bundle, and creates `num_history_shards` shards on first start, 512 by default. Temporal refuses a different count afterwards. `dynamic_config` sets Temporal dynamic configuration and refuses the Worker Versioning V1 keys. The ECS health check requests `/api/v1/namespaces/temporal-system` over HTTPS on port 7243 and verifies it against the CA.

Security groups:

| Group | Admits |
|---|---|
| client | Nothing. A workload attaches it to reach the frontend, and its egress allows port 7233 to the NLB |
| NLB | Port 7233 from the client group |
| server | Port 7233 from the NLB, and ports 6933-6939 and 7233-7239 from other server tasks |
| admin-tools | Nothing |
| UI load balancer | Port 443 from `ui_allowed_cidr_blocks` |
| UI | Port 8080 from its load balancer |

Every task group reaches the VPC endpoints and S3 over 443, and the server and admin-tools groups reach the database CIDRs on 5432.

## Deploy

Terraform registers the task definitions and creates the server service at a count of zero, and it leaves the service's task definition and count to `inv temporal-deploy`. The task runs the admin-tools `schema` action and stops unless it exits 0, moves the service to the task definition Terraform last registered at `server_count`, waits for it to settle, and runs the `namespace` action, which registers `namespace` with `namespace_retention`. A server version change therefore applies its schema before any server of that version starts. Run it after the first apply and after every apply that changes an image.

The secret store redeploys the server after a rotation of the `temporal` role password when `server_service_arn` is in its `restart_services` for `database_temporal`, which the `private-bedrock` root sets.

## UI

The UI runs Temporal UI 2.58.0. It connects to the frontend through the NLB over TLS with host verification and serves HTTPS on port 8080 with a certificate it issues at start. The load balancer does not verify that certificate, and the UI key never leaves the task. The load balancer presents `ui_acm_certificate_arn` under the TLS 1.3 FIPS policy. `inv temporal-certs generate --ui-name NAME` issues a certificate for it from the generated CA, which `aws acm import-certificate` imports.

Operators reach the ALB from a security group in `ui_operator_security_group_ids`, to which the module adds egress on 443 to the ALB, or from a network in `ui_allowed_cidr_blocks`, such as a VPN client range. The `private-bedrock` root passes the jumphost task group. `inv temporal-ui` starts a jumphost task and forwards `localhost:8443`, or `--port`, to the ALB until it is interrupted. The browser verifies the ALB certificate against the name it was issued for, so map that name to `127.0.0.1` in `/etc/hosts`, trust the CA that issued it, and open `https://<certificate name>:8443`.

## Images

`docker/temporal.Dockerfile` builds the server image, `docker/temporal-admin-tools.Dockerfile` the admin-tools image, and `docker/temporal-ui.Dockerfile` the UI image. Each starts from the upstream digest the local stack pins in `compose.yaml`.

| Image | Upstream base | Additions |
|---|---|---|
| server | `temporalio/server:1.32.1@sha256:acecbab5b0b8ae9cf4012b508866ca97feda3709577bf157b537239f40029049` | RDS CA bundle, health probe, start script, and the configuration template from `v1.32.1` checked against SHA-256 `8dc9ea0acf7f94b609fa4d1a11aa6d3c3e2c8afd8e7c6c72e50728d4f1fe1129` |
| admin-tools | `temporalio/admin-tools:1.32.1@sha256:9ef0dc5b063bf4ffd5c1512de72e0e73e225bd2c7006a18ce69bd592a8862a83` | RDS CA bundle and `docker/temporal-admin-tools.sh` |
| UI | `temporalio/ui:2.58.0@sha256:d36185e811d0ce1720b8d3ca4d9ac4dac504b67181e9b5e3d3ca72b2294f3590` | `openssl` and the start script that issues the UI server certificate |

The server and admin-tools images add the RDS global CA bundle at `/etc/ssl/rds-global-bundle.pem`. The build fetches the bundle from `RDS_CA_BUNDLE_URL` and fails unless its SHA-256 matches `RDS_CA_BUNDLE_SHA256`. Set both build arguments to use a regional or GovCloud bundle. RDS publishes a new bundle when it adds a CA, so a bundle bump updates the checksum in both Dockerfiles. The admin-tools image also carries `docker/temporal-admin-tools.sh` as its entrypoint, with `schema` as the default command and `namespace` as the other action.

`inv publish-temporal-images` builds the three images for `linux/arm64`, or `--arch amd64`, and pushes them. Each tag is `<upstream version>-<content hash>-<arch>`, where the hash covers the Dockerfile and every file it copies. An image whose tag is already in its repository is not rebuilt. The task prints each image reference with its digest.

The admin-tools task connects to Aurora with `SQL_TLS=true` and `SQL_TLS_CA_FILE=/etc/ssl/rds-global-bundle.pem`. `temporal-sql-tool` verifies the certificate against that bundle and the host name against `SQL_HOST`, so a connection to the cluster endpoint fails on any other CA.

## Outputs

| Output | Value |
|---|---|
| `endpoint` | `host:port` of the frontend NLB that clients dial. Empty until the images are set |
| `namespace` | Namespace the workers use |
| `frontend_server_name` | Name clients verify the frontend certificate against, their `TEMPORAL_TLS_SERVER_NAME` |
| `client_ca_secret_arn` | Secret clients inject as `TEMPORAL_TLS_CA`. Null when the frontend chains to a public root |
| `client_security_group_id` | Security group a workload attaches to reach the frontend. Null until the images are set |
| `server_service_arn` | Server ECS service, for `secret_restart_services`. Null until the images are set |
| `execution_role_arns` | Execution role of each task, keyed by `server`, `admin_tools`, and `ui` |
| `deploy` | Cluster, server service and count, task definitions, subnets, and security groups `inv temporal-deploy` reads. Null until the images are set |
| `ui_url` | HTTPS address of the UI ALB. Null without the UI |
| `server_repository_url`, `admin_tools_repository_url`, `ui_repository_url` | ECR repositories `inv publish-temporal-images` pushes to |
| `server_repository_name`, `admin_tools_repository_name` | Names of the server and admin-tools repositories |
| `frontend_certificate_source` | `acm` when the NLB presents `frontend_acm_certificate_arn`, otherwise `secrets` |
| `tls_ca_secret_arn`, `tls_internode_secret_arn`, `tls_frontend_secret_arn` | TLS secrets `inv temporal-certs put` writes. The frontend secret is null in ACM mode |
| `frontend_acm_certificate_arn`, `internode_acm_certificate_arn` | ACM certificates the NLB presents or that track the internode expiry |
| `certificate_expiry_topic_arn` | SNS topic that receives the ACM expiry events. Null when no certificate is tracked |

## Tests

`tests/temporal_control_plane.tftest.hcl` and `tests/deployment.tftest.hcl` run the module against a mocked provider. They assert that no task definition holds a secret value or a writable root filesystem, that only the server reads the TLS keys, that the frontend admits the client group only, that both load balancers write access logs, the ACM frontend listener, and the input checks. Run them with `inv terraform-test`, which CI also runs.
