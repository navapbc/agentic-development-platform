# private-bedrock

Reference composition root that stands up the platform's AWS network, the private Bedrock path, the Fargate jumphost, and the data tier for a single account. It wires the core infrastructure modules (`vpc-endpoints`, `ecr`, `cloudtrail`, `egress`, `dns-firewall`), the application modules (`bedrock-iam`, `bedrock-jumphost-fargate`), and the data tier modules (`aurora-postgresql`, `elasticache`, `clickhouse-storage`, `langfuse-blob-store`) into one apply.

It works from zero or against existing infrastructure. Set `create_network = true` to provision a tiered private VPC, or set it false and supply `vpc_id`, `subnet_ids`, `endpoint_allowed_cidrs`, and the existing tier subnets. Each core piece has its own toggle, so an account that already has VPC endpoints, a registry, an organization CloudTrail, or organization egress can skip them. The data tier services are off until enabled.

## State

State is stored in the S3 bucket created by the [bootstrap](../../bootstrap) root, with S3 native locking. Write `backend.hcl` from the bootstrap output, or copy `backend.hcl.example` and fill in the bucket. Every `terraform init` passes it:

```
terraform init -backend-config=backend.hcl
```

A working directory that was applied before the backend existed migrates its local state once with `terraform init -migrate-state -backend-config=backend.hcl`. See the [bootstrap README](../../bootstrap/README.md).

## Usage

Copy an example tfvars file, fill in the placeholders, then apply under the platform profile. `access_log_bucket` is required and names the bucket that receives S3 server access logs from the CloudTrail, jumphost exec-log, and Langfuse buckets. The bootstrap root creates it and prints it with `terraform output -raw access_log_bucket`.

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
| Management | `10.0.21.0/24` | The jumphost and administrative tasks such as database role bootstrap | Through the egress path |
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

## After apply

1. Push the jumphost image to the repository shown in the `jumphost_ecr_repository_url` output with `inv publish-jumphost-image --tag v1`.
2. Attach the `bedrock_invoke_policy_arn` output to the workload role that signs Bedrock inference.
3. Bring up the platform and open the tunnel with `inv tunnel-start --detach`.

## Toggles for existing infrastructure

| Variable | Set false when |
|----------|----------------|
| `create_network` | A VPC and private subnets already exist. Then set `vpc_id`, `subnet_ids`, and `endpoint_allowed_cidrs`, plus `management_subnet_ids`, `management_cidr_blocks`, and `data_subnet_ids` for the tiers. |
| `create_endpoints` | The account already has the bedrock-runtime, ssmmessages, ECR, logs, and kms endpoints. |
| `create_ecr_repository` | A registry already holds the jumphost image. Then set `jumphost_repository_name`. |
| `create_cloudtrail` | An organization CloudTrail already covers this account. |
| `create_dns_firewall` | The organization already filters DNS for the VPC. |
