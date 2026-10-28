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

Stop conditions. The secret entries are provisioned with KMS encryption and least-privilege read policies, rotation is enabled where supported, and `terraform validate` passes. The module is toggleable. The role bootstrap and the rotation run without CloudFormation and without a container image.

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
- R6. A role bootstrap, run through the RDS Data API with the RDS-managed master secret, creates one database role and database per service (the platform application, its migrator, Temporal, and Langfuse), runs `CREATE EXTENSION pgaudit`, and grants each role only its own database, so no service uses the master credential. The platform service roles also get `SELECT` on `agent_platform.alembic_version`, which the startup schema check reads, and only the migrator role holds DDL rights.
- R7. Each per-service database credential is a Secrets Manager secret with rotation enabled.
- R8. The Secrets Manager rotation function for ElastiCache users is attached to the `elasticache` module's user secret, which already holds `username`, `password`, and `user_arn`.
- R9. Rotation runs in an after-hours window, and the services that read a secret redeploy after the secret changes, so a running service picks up the new value.
- R10. Terraform generates every secret value it can. Secrets issued by an external system are created empty and set out of band.
- R11. The workers accept the review and authorship signing keys as environment values, so a deployment injects them from the secret store in place of a mounted file.

### Scope Boundaries

- This module provisions the secret entries and their access policies. It generates the values Terraform can generate. A deployment supplies the externally issued values out of band.
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
- KTD4. Write the rotation and bootstrap function in the repository and package it with the Terraform archive provider. Governs R6, R7, R8. Rationale. The AWS rotation templates deploy only as CloudFormation stacks from the Serverless Application Repository, and the platform does not add CloudFormation on top of Terraform. A boto3-only function needs no vendored dependency.
- KTD5. Run the PostgreSQL bootstrap and password changes through the RDS Data API as the master user. Governs R6, R7. Rationale. No client needs a network path to the database port, no database driver or container image is needed, and calls are authorized by IAM and the master secret. Passwords are sent as SCRAM-SHA-256 verifiers, so the DDL statement log never carries a password.
- KTD6. Rotate each PostgreSQL role in place and keep two passwords on the ElastiCache user. Governs R7, R8. Rationale. A second clone role per service would leave objects owned by the clone after a migration. A PostgreSQL role holds one password, so a consumer that has not restarted cannot open a new connection until its redeploy, which the after-hours window bounds. A Valkey user holds two passwords, so the cache rotation has no gap.
- KTD7. Schedule rotation on the first Sunday of every third month at 06:00 UTC with a three-hour window. Governs R4, R9. Rationale. FedRAMP Rev5 IA-5(f) leaves the period organization-defined, and IA-5(1) restricts forced periodic change only for user memorized secrets. A quarterly period is at most 92 days.
- KTD8. Redeploy consumers through an EventBridge rule on `RotationSucceeded` and on an operator `PutSecretValue`, targeting an SSM Automation document that calls ECS `UpdateService` with `forceNewDeployment`. Governs R9. Rationale. The redeploy needs no code, and the same rule covers an operator update of an external secret. The rule takes the ECS service ARNs as input, which the container-platform feature supplies.
- KTD9. Write generated values through write-only arguments from ephemeral resources. Governs R10. Rationale. The values never enter the plan or state. Passwords come from the Secrets Manager `GetRandomPassword` API, the same generator the rotation function uses.

### High-Level Design

A `secret-store` module provisions a KMS key for the platform secrets and a Secrets Manager entry per platform secret, each encrypted with that key and carrying a resource policy that grants read only to the consuming workload role passed in and denies every other reader. A repo-owned Python function in the management tier rotates the per-service PostgreSQL credentials through the RDS Data API and the ElastiCache user through `ModifyUser`, reaching both APIs and Secrets Manager through interface endpoints. The same PostgreSQL function runs the role bootstrap when `inv db-bootstrap-roles` invokes it. EventBridge rules redeploy the consuming ECS services after a change. The README documents the entries that a deployment rotates manually and the event triggers. The module is gated by a create toggle and emits the secret ARNs for the compute layer to inject.

### Sequencing

U1 adds the secret-store module. U2 wires it into the composition root optionally. U3 adds the database credentials, the rotation, and the redeploy. U2 and U3 depend on U1.

## Implementation Units

### U1. Secret store module

Goal. Provision the platform secret entries with KMS encryption, scoped read, and rotation where supported.
Requirements. R1, R2, R3, R4, R5, R10.
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
Requirements. R6, R7, R8, R9, R11.
Files. The secret store module and its function source, the `aurora-postgresql` Data API toggle, the `elasticache` write-only credential, `inv db-bootstrap-roles`, the signing key settings, and the README.
Approach. Enable the Data API on the cluster. Create a rotating secret per role. Run the role bootstrap through the function with the master secret. Attach the ElastiCache rotation to the cache user secret. Wire the redeploy rules for the consumer services passed in.
Test scenarios. Each service connects as its own role and cannot reach another service's database. The application role reads `alembic_version` and cannot write it or create objects. A forced rotation of each secret succeeds and the consumer reconnects after its redeploy.
Verification. `terraform validate`, `terraform plan`, and a rotation exercise in a sandbox account.

