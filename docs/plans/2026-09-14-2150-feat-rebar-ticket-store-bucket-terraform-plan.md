---
title: Rebar Ticket Store Bucket Terraform - Plan
type: feat
date: 2026-09-14
origin: docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-brainstorm
execution: infra
---

# Rebar Ticket Store Bucket Terraform - Plan

## Goal Capsule

Objective. The rebar ticket store S3 bucket is provisioned with KMS encryption, TLS-only access, a public-access block, versioning, and isolation from the evidence and audit writers, so the ticket store conforms to the platform-owned FedRAMP Medium controls.

Means. Add a toggleable terraform module for the rebar ticket bucket that mirrors the hardened-bucket pattern, encrypts with KMS, denies non-TLS access, blocks public access, enables versioning, and scopes access to the rebar reader and writer roles.

Product authority. This plan enriches the FedRAMP readiness scope of the roadmap at [2026-09-02-1801-feat-remaining-work-roadmap-plan.md](2026-09-02-1801-feat-remaining-work-roadmap-plan.md). It provisions the rebar ticket bucket target the local-to-AWS mapping names.

Stop conditions. The bucket is provisioned with encryption, TLS-only access, a public-access block, versioning, and role-scoped access, and `terraform validate` passes. The module is toggleable.

## Product Contract

### Summary

The v0.1.0 rebar backend keeps the ticket store on a git orphan branch synced to a private S3 bucket, but no terraform provisions that bucket, so its encryption, TLS, and access isolation are not applied by infrastructure as code. This plan provisions the bucket with KMS encryption, a TLS-only bucket policy, a public-access block, versioning, and an access policy scoped to the rebar reader and writer roles, keeping the store isolated from the evidence and audit writers.

### Problem Frame

The ticket store holds work items and their status. FedRAMP Medium expects SC-28 encryption, SC-8 TLS, SC-7 isolation, and AC-6 least privilege for it, and the ticket-store credential isolation the platform already documents expects the read and write roles to be distinct. AWS provides the bucket controls as configuration, so the platform's responsibility is the terraform that provisions the bucket with the compliant settings and the role-scoped policy.

### Requirements

- R1. A terraform module provisions the rebar ticket S3 bucket with KMS encryption at rest and a TLS-only bucket policy.
- R2. The bucket blocks public access and enables versioning.
- R3. The bucket access policy scopes reads and writes to the rebar reader and writer roles, denying cross-use by the evidence and audit writers.
- R4. The module is toggleable so an account that reuses an existing ticket bucket provisions nothing.

### Scope Boundaries

- This module provisions the bucket. It does not initialize the rebar store, which the rebar backend and its sync own.
- The reader and writer roles are defined by the compute layer and referenced here.
- The evidence bucket is a separate module, and this bucket stays isolated from it.

### Sources

- Roadmap and mapping: [2026-09-02-1801-feat-remaining-work-roadmap-plan.md](2026-09-02-1801-feat-remaining-work-roadmap-plan.md), [local-to-aws-mapping.md](../local-to-aws-mapping.md).
- Credential isolation: `docs/tickets-store-credentials.md`.
- Hardened-bucket pattern: `infra/terraform/evidence-store`, `infra/terraform/cloudtrail`.

## Planning Contract

### Key Technical Decisions

- KTD1. Mirror the hardened-bucket pattern from the evidence-store module. Governs R1, R2. Rationale. The pattern already establishes encryption, TLS-only, public-access block, and versioning, so reuse keeps the posture consistent.
- KTD2. Scope access to the rebar reader and writer roles and deny the evidence and audit writers. Governs R3. Rationale. The credential isolation the platform documents requires the ticket store to be separate from the evidence tier.
- KTD3. Make the module toggleable. Governs R4. Rationale. It follows the repo pattern and lets an account reuse an existing bucket.

### High-Level Design

A `rebar-ticket-store` module provisions the bucket with KMS encryption, a TLS-only and role-scoped bucket policy, a public-access block, ownership controls, and versioning, mirroring the evidence-store module. The access policy admits the rebar reader for reads and the rebar writer for writes and denies the evidence and audit writer roles. The module is gated by a create toggle and emits the bucket name and ARN. The composition root gains optional wiring.

### Sequencing

U1 adds the module. U2 wires it into the composition root optionally. U2 depends on U1.

## Implementation Units

### U1. Rebar ticket store bucket module

Goal. Provision the ticket bucket with encryption, TLS-only access, public-access block, versioning, and role-scoped access.
Requirements. R1, R2, R3, R4.
Files. `infra/terraform/rebar-ticket-store/` (new module with versions, variables, main, outputs, README).
Approach. Provision the bucket with KMS encryption, a public-access block, ownership controls, versioning, and a bucket policy that denies non-TLS access and scopes reads and writes to the rebar reader and writer roles while denying the evidence and audit writers. Gate on a create toggle. Emit the bucket name and ARN.
Test scenarios. `terraform validate` passes. The bucket declares KMS encryption, a TLS-only policy, a public-access block, and versioning. The policy scopes access to the rebar roles. The module provisions nothing when disabled.
Verification. `terraform validate`, module review.

### U2. Wire the ticket bucket into the composition root

Goal. Make the composition root provision the ticket bucket optionally.
Requirements. R4.
Files. `infra/terraform/examples/private-bedrock/` and its README.
Approach. Add an optional rebar-ticket-store module block gated by its toggle, passing the rebar reader and writer roles, and surface the bucket name as an output.
Test scenarios. `terraform validate` passes with the bucket enabled and disabled. The bucket name surfaces as an output when enabled.
Verification. `terraform validate`, composition review.
