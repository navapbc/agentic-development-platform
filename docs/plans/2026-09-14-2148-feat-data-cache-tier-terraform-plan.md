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

Objective. The platform's PostgreSQL database and Redis cache are provisioned as managed AWS services with encryption, least-privilege access, backups, and private networking, so the data tier conforms to the platform-owned FedRAMP Medium controls.

Means. Add toggleable terraform modules for Amazon RDS for PostgreSQL and Amazon ElastiCache for Redis, each with KMS encryption at rest, TLS in transit, private subnet placement, a scoped security group, automated backups, and multi-AZ.

Product authority. This plan enriches the FedRAMP readiness scope of the roadmap at [2026-09-02-1801-feat-remaining-work-roadmap-plan.md](2026-09-02-1801-feat-remaining-work-roadmap-plan.md). It provisions the data-tier targets the local-to-AWS mapping and hardening design name.

Stop conditions. RDS and ElastiCache are provisioned with encryption, private networking, backups, and multi-AZ, and `terraform validate` passes. The modules are toggleable so an account that reuses an existing data tier provisions nothing.

## Product Contract

### Summary

The platform runs PostgreSQL and Redis in compose locally, but no terraform provisions their AWS targets, so the data tier has no infrastructure-enforced encryption, backup, or network isolation. This plan adds an RDS for PostgreSQL module and an ElastiCache for Redis module, each hardened for encryption at rest through KMS, TLS in transit, placement in private subnets behind a scoped security group, automated backups with a retention window, and multi-AZ for availability.

### Problem Frame

The data tier holds the execution state, the audit index, and the coordination state. FedRAMP Medium expects SC-28 encryption at rest, SC-8 encryption in transit, SC-7 network isolation, and CP backup for this tier. AWS provides these as managed-service configuration, so the platform's responsibility is the terraform that provisions RDS and ElastiCache with the compliant settings.

### Requirements

- R1. An RDS for PostgreSQL module provisions the database with KMS encryption at rest, TLS enforced in transit, private subnet placement, a scoped security group, automated backups, and multi-AZ.
- R2. An ElastiCache for Redis module provisions the cache with encryption at rest and in transit, private subnet placement, and a scoped security group.
- R3. Both modules are toggleable so an account that reuses an existing data tier provisions nothing.
- R4. Both modules emit the connection endpoints and the security group ids as outputs for the consuming compute layer.

### Scope Boundaries

- These modules provision the managed data services. They do not migrate data or run schema migrations, which the db-migrate path owns.
- Secret material such as the database password is provisioned by the secret store feature and consumed here by reference.
- The modules follow the toggleable, reusable pattern of the existing terraform modules.

### Sources

- Roadmap, mapping, and hardening design: [2026-09-02-1801-feat-remaining-work-roadmap-plan.md](2026-09-02-1801-feat-remaining-work-roadmap-plan.md), [local-to-aws-mapping.md](../local-to-aws-mapping.md), [fedramp-medium-hardening-design.md](../fedramp-medium-hardening-design.md).
- Module conventions: `infra/terraform/ecr`, `infra/terraform/evidence-store`, `infra/terraform/vpc-endpoints`.

## Planning Contract

### Key Technical Decisions

- KTD1. Use managed RDS and ElastiCache rather than self-hosted databases. Governs R1, R2. Rationale. The managed services carry the inherited AWS controls and expose the encryption, backup, and multi-AZ settings the platform must configure.
- KTD2. Place both in private subnets behind scoped security groups. Governs R1, R2. Rationale. SC-7 requires the data tier to be unreachable from outside the VPC.
- KTD3. Make the modules toggleable and output the endpoints. Governs R3, R4. Rationale. The modules follow the repo pattern and feed the compute layer without hardcoding.

### High-Level Design

An `rds-postgres` module provisions the instance or cluster with a KMS-encrypted storage, enforced TLS, a DB subnet group in private subnets, a security group that admits the database port only from the compute security group, automated backups with a retention window, and multi-AZ. An `elasticache-redis` module provisions the replication group with encryption at rest and in transit, a subnet group in private subnets, and a scoped security group. Both are gated by a create toggle and emit their endpoints and security group ids. The private-bedrock composition root gains optional wiring for both.

### Sequencing

U1 adds the RDS module. U2 adds the ElastiCache module. U3 wires both into the composition root optionally. The units are independent except U3.

## Implementation Units

### U1. RDS for PostgreSQL module

Goal. Provision the database with encryption, private networking, backups, and multi-AZ.
Requirements. R1, R3, R4.
Files. `infra/terraform/rds-postgres/` (new module with versions, variables, main, outputs, README).
Approach. Provision the instance or cluster with KMS storage encryption, enforced TLS, a DB subnet group in private subnets, a security group admitting the database port only from the compute security group, automated backups with a retention window, and multi-AZ. Gate on a create toggle. Emit the endpoint and security group id.
Test scenarios. `terraform validate` passes. The database declares KMS encryption, backups, and multi-AZ. The security group admits only the compute source. The module provisions nothing when disabled.
Verification. `terraform validate`, module review.

### U2. ElastiCache for Redis module

Goal. Provision the cache with encryption and private networking.
Requirements. R2, R3, R4.
Files. `infra/terraform/elasticache-redis/` (new module with versions, variables, main, outputs, README).
Approach. Provision the replication group with encryption at rest and in transit, a subnet group in private subnets, and a security group admitting the cache port only from the compute security group. Gate on a create toggle. Emit the endpoint and security group id.
Test scenarios. `terraform validate` passes. The cache declares encryption at rest and in transit. The security group admits only the compute source. The module provisions nothing when disabled.
Verification. `terraform validate`, module review.

### U3. Wire the data tier into the composition root

Goal. Make the composition root provision the data tier optionally.
Requirements. R3, R4.
Files. `infra/terraform/examples/private-bedrock/` and its README.
Approach. Add optional module blocks for RDS and ElastiCache gated by their toggles, passing the private subnets and the compute security group, and surface their endpoints as outputs.
Test scenarios. `terraform validate` passes with the data tier enabled and disabled. The endpoints surface as outputs when enabled.
Verification. `terraform validate`, composition review.
