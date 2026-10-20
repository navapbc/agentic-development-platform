---
title: Secret Store Terraform - Plan
type: feat
date: 2026-09-14
origin: docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-brainstorm
execution: infra
---

# Secret Store Terraform - Plan

## Goal Capsule

Objective. The platform's secret material lives in a managed secret store with KMS encryption, least-privilege access, and rotation where supported, so secrets conform to the platform-owned FedRAMP Medium controls rather than living in files.

Means. Add a toggleable terraform module that provisions the secret entries the platform needs in AWS Secrets Manager, encrypts them with KMS, grants read access only to the consuming workload roles, and enables rotation where the secret supports it.

Product authority. This plan enriches the FedRAMP readiness scope of the roadmap at [2026-09-02-1801-feat-remaining-work-roadmap-plan.md](2026-09-02-1801-feat-remaining-work-roadmap-plan.md). It provisions the secret-store target the local-to-AWS mapping and hardening design name.

Stop conditions. The secret entries are provisioned with KMS encryption and least-privilege read policies, rotation is enabled where supported, and `terraform validate` passes. The module is toggleable.

## Product Contract

### Summary

Platform secrets live in `.env` and the `config/local/*.yaml` files locally, and the local-to-AWS mapping records that they map to a managed secret store in a deployed environment, but no terraform provisions it. This plan provisions the secret entries the platform needs in AWS Secrets Manager, encrypts them with a KMS key, grants read access only to the consuming workload roles, and enables rotation where the secret type supports it. The signing keys, the model API keys, and the webhook secrets are the entries a deployment provisions this way. The trust-root bundle holds only public keys and is not a secret.

### Problem Frame

FedRAMP Medium expects authenticator and secret material to be centrally managed, encrypted, access-scoped, and rotated. AWS Secrets Manager provides this as managed-service configuration, so the platform's responsibility is the terraform that provisions the entries with the compliant settings and grants least-privilege read to the workload roles.

### Requirements

- R1. A terraform module provisions the platform's secret entries in AWS Secrets Manager.
- R2. Each entry is encrypted with a KMS key.
- R3. Read access to each entry is granted only to the consuming workload role.
- R4. Rotation is enabled for the entries whose type supports it, and the entries without rotation support are documented.
- R5. The module is toggleable so an account that manages secrets elsewhere provisions nothing.
- R6. A role bootstrap task, run from the management tier with the RDS-managed master secret, creates one database role and database per service (the platform application, its migrator, Temporal, and Langfuse), runs `CREATE EXTENSION pgaudit`, and grants each role only its own database, so no service uses the master credential. The platform service roles also get `SELECT` on `agent_platform.alembic_version`, which the startup schema check reads, and only the migrator role holds DDL rights.
- R7. Each per-service database credential is a Secrets Manager secret with rotation enabled.
- R8. The Secrets Manager rotation function for ElastiCache users is attached to the `elasticache` module's user secret, which already holds `username`, `password`, and `user_arn`.

### Scope Boundaries

- This module provisions the secret entries and their access policies. It does not set the secret values, which a deployment supplies out of band.
- The trust-root bundle is non-secret configuration and is out of scope.
- The workload roles that read the secrets are defined by the compute layer and referenced here.

### Sources

- Roadmap, mapping, and hardening design: [2026-09-02-1801-feat-remaining-work-roadmap-plan.md](2026-09-02-1801-feat-remaining-work-roadmap-plan.md), [local-to-aws-mapping.md](../local-to-aws-mapping.md), [fedramp-medium-hardening-design.md](../fedramp-medium-hardening-design.md).
- Secret surface: `.env.example` and `scripts/config.py` (signing key paths, model API keys, webhook secrets).
- Module conventions: `infra/terraform/evidence-store`, `infra/terraform/ecr`.

## Planning Contract

### Key Technical Decisions

- KTD1. Provision entries in Secrets Manager rather than Parameter Store. Governs R1, R4. Rationale. Secrets Manager carries native rotation and a first-class secret type, which the FedRAMP posture favors for authenticators.
- KTD2. Encrypt with a dedicated KMS key and scope read by role. Governs R2, R3. Rationale. A dedicated key with a scoped policy enforces least privilege at both the key and the secret.
- KTD3. Enable rotation where supported and document the rest. Governs R4. Rationale. Not every secret has a rotation lambda, so the honest posture enables it where possible and records the exceptions.

### High-Level Design

A `secret-store` module provisions a KMS key for the platform secrets and a Secrets Manager entry per platform secret, each encrypted with that key and carrying a resource policy that grants read only to the consuming workload role passed in. Rotation is enabled for the entries whose type supports a rotation schedule, and the README documents the entries that a deployment rotates manually. The module is gated by a create toggle and emits the secret ARNs for the compute layer to mount.

### Sequencing

U1 adds the secret-store module. U2 wires it into the composition root optionally. U2 depends on U1.

## Implementation Units

### U1. Secret store module

Goal. Provision the platform secret entries with KMS encryption, scoped read, and rotation where supported.
Requirements. R1, R2, R3, R4, R5.
Files. `infra/terraform/secret-store/` (new module with versions, variables, main, outputs, README).
Approach. Provision a KMS key for the platform secrets and a Secrets Manager entry per secret, each encrypted with the key and carrying a read policy scoped to the consuming role. Enable rotation for the entries that support it. Gate on a create toggle. Emit the secret ARNs. Document the manual-rotation entries in the README.
Test scenarios. `terraform validate` passes. Each entry declares KMS encryption and a scoped read policy. Rotation is enabled where supported. The module provisions nothing when disabled.
Verification. `terraform validate`, module review.

### U2. Wire the secret store into the composition root

Goal. Make the composition root provision the secret store optionally.
Requirements. R5.
Files. `infra/terraform/examples/private-bedrock/` and its README.
Approach. Add an optional secret-store module block gated by its toggle, passing the workload role, and surface the secret ARNs as outputs for the compute layer.
Test scenarios. `terraform validate` passes with the secret store enabled and disabled. The secret ARNs surface as outputs when enabled.
Verification. `terraform validate`, composition review.

### U3. Per-service database roles and credential rotation

Goal. Remove the master credential from service use and rotate every data-tier credential.
Requirements. R6, R7, R8.
Files. The secret store module, a role bootstrap task definition, and the README.
Approach. Run the role bootstrap from the management tier with the master secret. Create a rotating secret per role. Attach the ElastiCache rotation function to the cache user secret.
Test scenarios. Each service connects as its own role and cannot reach another service's database. A forced rotation of each secret succeeds and the consumer reconnects.
Verification. `terraform validate`, `terraform plan`, and a rotation exercise in a sandbox account.

