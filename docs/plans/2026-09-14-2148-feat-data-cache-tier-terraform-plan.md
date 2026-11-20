---
title: Data and Cache Tier Terraform - Plan
type: feat
date: 2026-09-14
origin: docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-brainstorm
execution: infra
---

# Data and Cache Tier Terraform - Plan

## Goal Capsule

Objective. The platform's PostgreSQL database, its Valkey cache, the ClickHouse storage and upload bucket behind Langfuse, and the network they sit in are provisioned as AWS infrastructure with encryption, least-privilege access, backups, private placement, and a filtered egress path, so the data tier conforms to the platform-owned FedRAMP Medium controls and the CMS Technical Reference Architecture zones.

Means. Add toggleable Terraform modules for Aurora PostgreSQL, ElastiCache for Valkey, the ClickHouse EFS storage, the Langfuse upload bucket, the egress path, the DNS Firewall, and the remote state bucket, and give the composition root a tiered network. Move the local cache to the same engine the AWS tier runs and keep Langfuse compatible with a supported ClickHouse.

Product authority. This plan enriches the FedRAMP readiness scope of the roadmap at [2026-09-02-1801-feat-remaining-work-roadmap-plan.md](2026-09-02-1801-feat-remaining-work-roadmap-plan.md). It provisions the data-tier targets the local-to-AWS mapping and hardening design name.

Stop conditions. Every module validates, the composition root plans cleanly against the account with the data tier enabled and disabled, the engine versions are confirmed available in the region, and the local Valkey, ClickHouse, and Langfuse cutover is rehearsed end to end on copies of a developer's data.

## Product Contract

### Summary

The platform runs PostgreSQL, a cache, ClickHouse, and MinIO in compose locally. This plan provisions their AWS targets in a data tier that has no route to the internet, behind a network ACL and security groups that admit only the application and management tiers. The network gains public, application, management, and data tiers, an outbound path for GitHub and Jira, and DNS filtering. Terraform state moves to an encrypted remote backend, because the data tier puts secret material in state.

### Problem Frame

The data tier holds the execution state, the audit index, the coordination state, and the Langfuse traces. FedRAMP Medium expects SC-28 encryption at rest, SC-8 encryption in transit, SC-7 boundary protection with deny by default, and CP-9 backup for this tier. The CMS TRA requires data to be protected by at least three challenges on separate resources and forbids the data zone from originating connections outside itself. AWS provides these as managed-service configuration, so the platform's responsibility is the Terraform that provisions them with compliant settings.

### Requirements

- R1. An Aurora PostgreSQL module provisions the database with KMS encryption at rest, enforced TLS, IAM database authentication, an RDS-managed rotating master secret, private data-tier placement, a scoped security group with no egress, 30-day automated backups, and deletion protection. Storage is replicated across three Availability Zones. The compute instance count is configurable and defaults to one Serverless v2 instance that pauses when idle.
- R2. An ElastiCache module provisions Valkey with encryption at rest and TLS required, an RBAC user in place of the default user, the user credential in Secrets Manager in the rotation format, private data-tier placement, and a scoped security group with no egress. The node count is configurable and defaults to one.
- R3. A ClickHouse storage module provisions an encrypted EFS file system with a TLS-only, access-point-only file system policy and automatic backups, and a Langfuse bucket module provisions the encrypted, versioned, TLS-only upload bucket.
- R4. The composition root provides public, application, management, and data subnet tiers. The data tier spans two Availability Zones, has no default route, and sits behind a network ACL that admits only PostgreSQL, Valkey, and NFS from the application and management tiers.
- R5. An egress module gives the application and management tiers an outbound path selected by `egress_mode`, either none, a hardened NAT instance, or a NAT Gateway. A DNS Firewall module limits resolution to an allowlist and fails closed.
- R6. A state module and bootstrap root provide an encrypted, versioned, TLS-only state bucket, and the composition root stores its state there with S3 native locking.
- R7. Every module is toggleable so an account that reuses existing infrastructure provisions nothing, and each emits the endpoints, secret ARNs, and security group ids the compute layer consumes.
- R8. The local stack runs the same cache engine and RBAC user as AWS, and a developer moves an existing checkout to it with a pull and an up while the Langfuse queue survives.
- R9. Platform-owned configuration shared by every environment lives in a tracked `config/platform` directory, separate from the operator-edited files in `config/local`.

### Scope Boundaries

- These modules provision the managed data services and their network. They do not create per-service database roles or databases, which the secret store feature provisions with the master secret from the management tier.
- The ClickHouse task, the Langfuse services, the ingress for webhooks, and the off-hours scale-to-zero schedule belong to the container-platform services feature.
- Rotation functions for the cache credential and the per-service database credentials belong to the secret store feature.

### Sources

- Roadmap, mapping, and hardening design: [2026-09-02-1801-feat-remaining-work-roadmap-plan.md](2026-09-02-1801-feat-remaining-work-roadmap-plan.md), [local-to-aws-mapping.md](../local-to-aws-mapping.md), [fedramp-medium-hardening-design.md](../fedramp-medium-hardening-design.md).
- The CMS Technical Reference Architecture multi-zone architecture and its business rules BR-F-4 and RP-DSS-5.
- Module conventions: `infra/terraform/ecr`, `infra/terraform/evidence-store`, `infra/terraform/vpc-endpoints`.

## Planning Contract

### Key Technical Decisions

- KTD1. Use Aurora PostgreSQL in provisioned engine mode with Serverless v2 capacity, matching the database engine of the reference CMS environment. Governs R1. Rationale. Aurora storage replicates across three Availability Zones, and Serverless v2 scales to zero for evaluation stands.
- KTD2. Pin Aurora PostgreSQL 16.15. Governs R1. Rationale. It is the only 16.x release carrying every currently back-ported community CVE fix, and automatic minor upgrades keep it patched. 16.9 reaches end of standard support on 31 December 2026, and the 16.8 LTS line lacks the September 2026 fixes.
- KTD3. Let RDS manage the master password in Secrets Manager. Governs R1. Rationale. RDS generates and rotates it every seven days, and it never enters Terraform state.
- KTD4. Use ElastiCache for Valkey 9.1 with an RBAC user. Governs R2, R8. Rationale. ElastiCache offers no Redis OSS release after 7.1, Langfuse supports Valkey 8 and later, Langfuse's ioredis client passes `REDIS_USERNAME`, and Valkey 9.1 is the only version that loads the developers' existing Redis 7.4 snapshot.
- KTD5. Keep ClickHouse self-hosted on EFS, matching the reference CMS environment, because ClickHouse Cloud holds no FedRAMP authorization. Pin ClickHouse 26.9.19.9, the newest release line that receives security fixes, and Langfuse 4.59.0, because Langfuse 4.15.0 fails every trace insert on ClickHouse 26.8 and 26.9. Governs R3, R8.
- KTD6. Place the ClickHouse file system in the data tier and the ClickHouse task in the application tier. Governs R3, R4. Rationale. The task pulls images and ships logs through the endpoints, and the data zone may not originate connections.
- KTD7. Default every data-tier service to off, one instance or node, and a tiered network with egress through a NAT instance. Governs R4, R5, R7. Rationale. An evaluation stand pays the least, and production raises counts and switches to a NAT Gateway through variables.
- KTD8. Filter egress with Route 53 Resolver DNS Firewall and document AWS Network Firewall as the production upgrade. Governs R5. Rationale. DNS filtering gives deny by default at low cost, and Network Firewall adds SNI filtering where a client might connect by IP address.
- KTD9. Store state in S3 with native locking and keep the provider lock file out of the repository. Governs R6.

### High-Level Design

The composition root keeps its existing private subnet as the application tier and adds management, data, and public tiers. The data tier gets its own route table without a default route and a network ACL. The egress module adds the default route for the application and management route tables through a NAT instance or NAT Gateway in the public tier, and the DNS Firewall filters the VPC resolver. The jumphost moves to the management tier, and the endpoint security group and S3 gateway endpoint admit the management tier. Aurora, ElastiCache, the ClickHouse EFS mount targets, and their security groups sit in the data tier and admit only the application and management CIDRs. The Langfuse bucket and the state bucket are regional.

### Sequencing

U1 adds the state module and bootstrap root. U2 adds the network tiers, the egress module, and the DNS Firewall. U3 adds the Aurora module. U4 adds the ElastiCache module. U5 adds the ClickHouse storage and Langfuse bucket modules. U6 wires them into the composition root. U7 moves the local cache to Valkey, upgrades ClickHouse and Langfuse, and moves the platform configuration to `config/platform`. U8 updates the documentation.

## Implementation Units

### U1. Remote state

Goal. Store state encrypted, versioned, and locked.
Requirements. R6.
Files. `infra/terraform/tf-state/`, `infra/terraform/bootstrap/`, the composition root `versions.tf` and `backend.hcl.example`.
Verification. `terraform validate`.

### U2. Network tiers, egress, and DNS filtering

Goal. Separate the zones and give the private tiers a filtered outbound path.
Requirements. R4, R5.
Files. `infra/terraform/egress/`, `infra/terraform/dns-firewall/`, the composition root `network.tf`.
Verification. `terraform validate`, and `terraform plan` against the account showing additions and in-place updates only.

### U3. Aurora PostgreSQL module

Goal. Provision the database.
Requirements. R1, R7.
Files. `infra/terraform/aurora-postgresql/`.
Verification. `terraform validate`, `terraform plan`, and `aws rds describe-db-engine-versions` confirming 16.15 in the `aurora-postgresql16` family with `db.serverless` orderable.

### U4. ElastiCache for Valkey module

Goal. Provision the cache.
Requirements. R2, R7.
Files. `infra/terraform/elasticache/`.
Verification. `terraform validate`, `terraform plan`, and `aws elasticache describe-cache-engine-versions` confirming Valkey 9.1 in the `valkey9` family.

### U5. ClickHouse storage and Langfuse bucket modules

Goal. Provision the Langfuse storage.
Requirements. R3, R7.
Files. `infra/terraform/clickhouse-storage/`, `infra/terraform/langfuse-blob-store/`.
Verification. `terraform validate`, `terraform plan`.

### U6. Composition root wiring

Goal. Provision the data tier from the composition root.
Requirements. R4, R7.
Files. `infra/terraform/examples/private-bedrock/` and its README and tfvars examples.
Verification. `terraform plan` with the data tier enabled and disabled.

### U7. Local cutover

Goal. Run the AWS cache engine locally and keep Langfuse on a supported ClickHouse.
Requirements. R8, R9.
Files. `compose.yaml`, `.env.example`, `config/platform/`, `services/integration/credentials.py`, `services/orchestration/gates/builtin.py`, the affected tests.
Verification. A rehearsal of the new compose definition on copies of a developer's volumes, with no published ports, that loads the Redis snapshot into Valkey, runs a BullMQ job under the RBAC user, and lands an OTLP span in ClickHouse.

### U8. Documentation

Goal. Record the delivered posture.
Requirements. All.
Files. `README.md`, `CHANGELOG.md`, `docs/local-to-aws-mapping.md`, `docs/fedramp-medium-hardening-design.md`, `docs/fedramp-control-inheritance.md`, the module READMEs, and the container-platform and secret store plans.
