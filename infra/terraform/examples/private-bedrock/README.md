# private-bedrock

Reference composition root that stands up the platform's AWS network, the private Bedrock path, the Fargate jumphost, and the data tier for a single account. It wires the core infrastructure modules (`vpc-endpoints`, `ecr`, `cloudtrail`, `egress`, `dns-firewall`), the application modules (`bedrock-iam`, `bedrock-jumphost-fargate`), the data tier modules (`aurora-postgresql`, `elasticache`, `clickhouse-storage`, `langfuse-blob-store`), and the `secret-store` and `rebar-ticket-store` modules into one apply.

It works from zero or against existing infrastructure. Set `create_network = true` to provision a tiered private VPC, or set it false and supply `vpc_id`, `subnet_ids`, `endpoint_allowed_cidrs`, and the existing tier subnets. Each core piece has its own toggle, so an account that already has VPC endpoints, a registry, an organization CloudTrail, or organization egress can skip them. The data tier services and the secret store are off until enabled.

## State

State is stored in the S3 bucket created by the [bootstrap](../../bootstrap) root, with S3 native locking. Write `backend.hcl` from the bootstrap output, or copy `backend.hcl.example` and fill in the bucket. Every `terraform init` passes it:

```
terraform init -backend-config=backend.hcl
```

A working directory that was applied before the backend existed migrates its local state once with `terraform init -migrate-state -backend-config=backend.hcl`. See the [bootstrap README](../../bootstrap/README.md).

## Usage

Copy an example tfvars file, fill in the placeholders, then apply under the platform profile. `access_log_bucket` is required and names the bucket that receives S3 server access logs from the CloudTrail, jumphost exec-log, Langfuse, and Temporal load balancer log buckets. The bootstrap root creates it and prints it with `terraform output -raw access_log_bucket`.

Greenfield, create everything:

```
cp greenfield.tfvars.example greenfield.tfvars
terraform init -backend-config=backend.hcl
AWS_PROFILE=agentic-platform terraform apply -var-file=greenfield.tfvars
```

Existing network, reuse a VPC and subnets:

```
cp existing-network.tfvars.example existing-network.tfvars
terraform init -backend-config=backend.hcl
AWS_PROFILE=agentic-platform terraform apply -var-file=existing-network.tfvars
```

## Network tiers

A created network is divided into public, application, management, and data zones. Each tier is a separate subnet group with its own route table, so a zone boundary is enforced by routing, network ACLs, and security groups rather than by convention.

| Tier | Default CIDRs | Runs | Route to the internet |
|---|---|---|---|
| Public (presentation) | `10.0.31.0/24` | The NAT instance or NAT Gateway | Internet gateway. Created only when an egress path is selected. |
| Application | `new_subnet_cidrs`, `10.0.1.0/24` | The interface endpoints and the platform services | Through the egress path |
| Management | `10.0.21.0/24` | The jumphost and the secret rotation functions | Through the egress path |
| Data | `10.0.11.0/24`, `10.0.12.0/24` | Aurora, ElastiCache, and the ClickHouse file system | None |

The data tier spans two Availability Zones because Aurora requires it. Its network ACL admits only PostgreSQL (5432), Valkey (6379), and NFS (2049) from the application and management tiers, returns responses to them, and allows traffic within the tier. The data tier has no default route, so it cannot originate a connection outside the VPC.

## Egress

`egress_mode` sets the outbound path for the application and management tiers. A created network defaults to `nat_instance`, a hardened Amazon Linux 2023 NAT instance for low-cost testing. Set `nat_gateway` for production. An existing network defaults to `none` and must keep it, because its organization supplies egress. The [egress README](../../egress/README.md) covers the hardening and the trade-offs.

A created network also enables the Route 53 Resolver DNS Firewall, which resolves only an allowlist of AWS, GitHub, Atlassian, and Slack names and answers NXDOMAIN for everything else. Add the SMTP relay with `dns_firewall_additional_domains`. Set `dns_firewall_default_action = "ALERT"` to log instead of block during a trial. The `egress_public_ip` output is the address destinations see.

## Data tier

| Variable | Provisions |
|---|---|
| `create_aurora` | Aurora PostgreSQL 16.15, one Serverless v2 writer from 0 to 4 ACUs that pauses after an hour idle, with an RDS-managed rotating master secret |
| `create_cache` | ElastiCache for Valkey 9.1, one node, TLS required, with an RBAC `langfuse` user whose credential is in Secrets Manager |
| `create_clickhouse_storage` | The encrypted EFS file system and access point for the ClickHouse task |
| `create_langfuse_bucket` | The S3 bucket for Langfuse event and media uploads |

`aurora_instance_count`, `aurora_serverless_max_capacity`, and `cache_num_nodes` raise capacity and availability for production. `kms_key_arn` applies one customer managed key across the data tier, the NAT instance volume, and the log groups.

## Secret store

`create_secret_store = true` provisions the [secret-store](../../secret-store) module. It creates the platform secrets in Secrets Manager under a dedicated KMS key and adds the `secretsmanager` interface endpoint. With `create_aurora` it also enables the Aurora Data API, adds the `rds-data` endpoint, and creates the per-service database credentials with their rotation function in the management tier. With `create_cache` it encrypts the cache user secret with the same key, adds the `elasticache` endpoint, and rotates the cache user.

`secret_readers` grants read access per secret to the workload roles, and `secret_restart_services` names the ECS services to redeploy after each secret changes. The module README lists the secret keys, the rotation schedule, and the manual rotation runbook.

## Rebar ticket store

`create_rebar_ticket_store = true` provisions the [rebar-ticket-store](../../rebar-ticket-store) module, the bucket the rebar ticket store syncs to through `git-remote-s3`. It creates a dedicated KMS key and a reader and a writer role, trusted by `rebar_ticket_store_trusted_principals`. Set `rebar_ticket_store_existing_reader_role_arn` and `rebar_ticket_store_existing_writer_role_arn` to use roles the organization issues instead. `rebar_ticket_store_admin_principals` names the principals that seed the store and empty the bucket before a destroy. The store reaches S3 through the S3 gateway endpoint, so it needs no interface endpoint. With the trail enabled, the root also records object-level CloudTrail data events on the bucket.

## Platform ECS cluster

`create_ecs_cluster = true` provisions the [ecs-cluster](../../ecs-cluster) module, the Fargate cluster the platform owns and shares with other teams. It encrypts ECS Exec sessions and Fargate ephemeral storage under dedicated KMS keys and logs Exec sessions and Container Insights to encrypted log groups. A team attaches the `ecs_deployer_policy_arn` output to its deploy role to deploy into this cluster and no other, and lists the task and execution roles that role may pass in `ecs_deployer_passable_role_arns`. A task role attaches `ecs_exec_task_policy_arn` to accept ECS Exec sessions.

## Temporal control plane

`create_temporal_control_plane = true` provisions the [temporal-control-plane](../../temporal-control-plane) module. It creates the ECR repositories for the Temporal server and admin-tools images, which `inv publish-temporal-images` fills, and the TLS secrets, which `inv temporal-certs put` fills. `temporal_frontend_acm_certificate_arn` serves the frontend from an ACM certificate, `temporal_frontend_certificate_publicly_trusted` marks that certificate as chaining to a public root, and `temporal_internode_acm_certificate_arn` tracks the internode certificate in ACM. `temporal_frontend_server_name` and `temporal_internode_server_name` name the certificates, and `inv temporal-certs put` refuses a certificate that does not carry them. `temporal_fips_140_mode` runs the Temporal processes in Go's FIPS 140-3 mode. The `temporal_tls` output lists the secrets and the expiry topic. Setting `temporal_server_image` and `temporal_admin_tools_image` to the references `inv publish-temporal-images` prints deploys the server on the platform ECS cluster, and `temporal_ui_image` with `temporal_ui_acm_certificate_arn` deploys the UI, which needs two `management_subnet_cidrs`. The root adds the server and admin-tools execution roles to the `database_temporal` readers and the server service to its restart list. Run `inv temporal-deploy` after each apply that changes an image. The `temporal_client` output holds the endpoint, namespace, server name, CA secret, and security group a worker uses, and `temporal_ui_url` the UI address. The UI load balancer admits the jumphost task group, which `inv temporal-ui` forwards through, and any CIDRs in `temporal_ui_allowed_cidr_blocks`.

`create_container_services = true` provisions the [container-services](../../container-services) module. It creates the ECR repositories for the platform, repository-intelligence, collector, Langfuse web, Langfuse worker, and ClickHouse images, which `inv publish-platform-images` fills. The `container_repository_urls` output lists them. `inv publish-platform-images`, or the `images-<arch>` artifact of the `publish-images` workflow, writes the seven image references to the git-ignored `images.auto.tfvars.json` in this directory, which Terraform reads with no flag. Leave the image variables out of the tfvars file, because a `-var-file` value overrides the file. `create_github_image_publisher = true` adds the [github-image-publisher](../../github-image-publisher) role that the workflow assumes to push to those repositories, trusting `github_repository` on `github_publish_refs`. Set `github_repository_ids` to the owner and repository IDs when the repository's OIDC tokens carry GitHub's immutable subject. Set `github_oidc_provider_arn` to the account's existing GitHub provider, or leave it empty to create one, and read the role from the `github_image_publisher_role_arn` output. `container_internal_acm_certificate_arn` adds the internal load balancer that names coordination, repo-intelligence, the collector, and Langfuse under `container_internal_domain`, which needs two `new_subnet_cidrs`. `container_internal_allowed_cidr_blocks` admits networks beyond the client security group, and the `container_internal_endpoints` output lists the groups, target groups, and URLs.

With the internal load balancer, `container_platform_image`, `container_repo_intelligence_image`, and the Temporal control plane, the module also runs the platform services on the cluster: the workflow and review workers, each in a blue and a green slot, coordination, repository intelligence, and the index worker, with the `db-migrate` and `schedule-bootstrap` one-shot tasks. `container_config_dir` names the directory of the environment's config files, which the tasks receive as `PLATFORM_ENV_CONFIG`, and `container_platform_ca_file` the CA the clients trust for the internal names. The composition lists each execution role as a reader of the secrets it injects and of the Temporal CA, attaches `bedrock_invoke_policy_arn` to the review worker, and adds the `bedrock-runtime-fips` interface endpoint when `container_use_fips_endpoints` is true. `inv platform-deploy` reads the `container_deploy` output, and the `container_workspace` output lists the workspace file system and its access points.

With `create_cache`, `create_clickhouse_storage`, `create_langfuse_bucket`, the secret store, and the four `container_langfuse_web_image`, `container_langfuse_worker_image`, `container_clickhouse_image`, and `container_tls_sidecar_image` references, the module also runs Langfuse web and worker against the `langfuse` database role, the cache user, and the blob bucket, and ClickHouse on the `clickhouse-storage` file system. `container_langfuse_admin_email` names the administrator, `container_langfuse_public_url` the URL `inv langfuse-ui` forwards, and `container_langfuse_task_sizes` the task sizes. The composition admits the jumphost task group to the internal load balancer, adds the Langfuse and ClickHouse services to the secret store restart rules for the secrets they read, and lists only the workloads that connect to Temporal as readers of the Temporal CA. The `container_clickhouse` output names the secret `inv clickhouse-certs put` writes, and the `container_langfuse` output the Langfuse URLs.

`container_collector_image` with `container_tls_sidecar_image` runs the collector, and every platform task exports to it. With `container_collector_exports_enabled`, the default, the composition adds the `xray-fips` or `xray` and the `aps-workspaces` interface endpoints, the collector exports to X-Ray and to a Prometheus workspace, and with Langfuse it forwards the generation spans to Langfuse. `container_xray_encryption_enabled` sets the region's X-Ray encryption to the telemetry key, which changes it for every X-Ray user in the region. The `container_telemetry` output names the workspace and its endpoint.

`off_hours_enabled` provisions the [off-hours](../../off-hours) module for the platform and Temporal ECS services. The Temporal server and UI, ClickHouse, and the collector stop last and start first. `off_hours_timezone`, `off_hours_sleep_schedule`, and `off_hours_wake_schedule` set the window, and the `off_hours` output names the documents for a run outside the schedule. The composition also lists every service and worker slot under each secret it reads in the secret store restart rules, so a rotation redeploys all of its consumers.

## EKS cluster

`create_eks = true` provisions the [eks-cluster](../../eks-cluster) module in the application subnets, with the API endpoint private to the VPC and open on 443 to the jumphost task group only. Each principal in `eks_admin_principal_arns`, keyed by a static label, receives cluster admin through an access entry. `eks_kubernetes_version`, `eks_platform_node_group`, `eks_sandbox_node_group`, and `eks_addon_versions` pass through to the module, and `container_use_fips_endpoints` sets the Secrets Store CSI provider to the Secrets Manager FIPS endpoint. Create the cluster with `eks_network_policy_enforcing_mode = "standard"` and set `strict` once the kube-system and platform NetworkPolicies are applied, as the module README describes.

The composition creates Pod Identity roles for the `activity-worker`, `litellm`, `mcp-gateway`, and `inbound-webhook` service accounts and lists each as a reader of the secrets its pods mount. It attaches `bedrock_invoke_policy_arn` to the litellm role, adds the activity worker role to the rebar ticket store's trusted principals and to the Temporal CA readers, and lets the EFS CSI node driver mount the workspace `checkouts` access point through a policy limited to that access point. The workspace mount targets admit the cluster security group, which the nodes carry. Aurora, the cache, and ClickHouse admit the application subnets, which hold the pod addresses. A pod reaches the Temporal frontend and the internal load balancer through a SecurityGroupPolicy that attaches the Temporal and internal client groups, which the `eks` output names.

The root also adds the `eks-auth` interface endpoint and its name to the DNS Firewall allowlist, so the Pod Identity agent obtains credentials inside the VPC, and tags the public subnets with `kubernetes.io/role/elb` and the application subnets with `kubernetes.io/role/internal-elb` for the Load Balancer Controller. An internet-facing load balancer spans two Availability Zones, so `create_eks` needs two `public_subnet_cidrs`. The `eks` output holds the cluster name, endpoint, VPC, Pod Identity roles, security groups, workspace file system and access point, node scheduling label and taint, controller settings, and control plane log group.

## After apply

1. Push the jumphost image to the repository shown in the `jumphost_ecr_repository_url` output with `inv publish-jumphost-image --tag v1`.
2. Attach the `bedrock_invoke_policy_arn` output to any workload role outside the composition that signs Bedrock inference. The composition attaches it to the review worker.
3. Bring up the platform and open the tunnel with `inv tunnel-start --detach`.
4. With the secret store and Aurora enabled, create the per-service database roles before the first migration with `inv db-bootstrap-roles --function-name "$(terraform output -raw role_bootstrap_function_name)"`. Then set each external secret, such as `github_token`, with `aws secretsmanager put-secret-value`.

## Toggles for existing infrastructure

| Variable | Set false when |
|----------|----------------|
| `create_network` | A VPC and private subnets already exist. Then set `vpc_id`, `subnet_ids`, and `endpoint_allowed_cidrs`, plus `management_subnet_ids`, `management_cidr_blocks`, and `data_subnet_ids` for the tiers. |
| `create_endpoints` | The account already has the bedrock-runtime, ssmmessages, ECR, logs, and kms endpoints, plus secretsmanager, rds-data, and elasticache when the secret store is enabled, and bedrock-runtime-fips when the platform services run with `container_use_fips_endpoints`. Then set `existing_endpoints_security_group_id`. |
| `create_ecr_repository` | A registry already holds the jumphost image. Then set `jumphost_repository_name`. |
| `create_cloudtrail` | An organization CloudTrail already covers this account. |
| `create_dns_firewall` | The organization already filters DNS for the VPC. |
