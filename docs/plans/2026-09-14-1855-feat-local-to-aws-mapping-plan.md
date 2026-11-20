---
title: Local-to-AWS Mapping Document - Plan
type: feat
date: 2026-09-14
origin: docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-brainstorm
execution: docs
---

# Local-to-AWS Mapping Document - Plan

## Goal Capsule

Objective. A committed document maps each local platform component to its AWS target, so a reader can trace every local service and store to the AWS resource that hosts it in a deployed environment.

Means. Write one document under `docs/` that lists each local component from the compose stack and the config surface, names its AWS target, and links the terraform module that provisions that target where one exists.

Product authority. This plan enriches one feature from the roadmap at [2026-09-02-1801-feat-remaining-work-roadmap-plan.md](2026-09-02-1801-feat-remaining-work-roadmap-plan.md), the "Local-to-AWS mapping document" entry. It does not widen that feature's scope.

Stop conditions. The document is committed and every local component has a named AWS target. The document is a mapping reference, so it provisions nothing and changes no runtime code.

## Product Contract

Product Contract preservation. Sourced from the roadmap feature "Local-to-AWS mapping document". The requirements below restate that feature's subtask as R-IDs with no scope change.

### Summary

The mapping from each local component to its AWS target exists only as plan prose today. Since the roadmap captured that state, v0.1.0 committed terraform modules that provision several of the AWS targets, so the mapping now consolidates existing module documentation rather than describing targets that have no code. The document names each local component, its AWS target, and the terraform module that provisions the target where one exists.

### Problem Frame

A reader who runs the local compose stack has no single reference that says where each component lives in AWS. The compose stack runs Temporal, Postgres, MinIO, LiteLLM, the workers, the repository-intelligence services, and the observability stack. The config surface in `scripts/config.py` names the Bedrock path, the connectivity provider, and the object store. The terraform under `infra/terraform` provisions the VPC endpoints, the container registry, the audit trail, the Bedrock IAM policy, and the ephemeral jump-host. The mapping ties these together.

### Requirements

- R1. A committed document under `docs/` maps each local component to its AWS target.
- R2. Each mapping row names the local component, the AWS target that hosts it in a deployed environment, and the terraform module that provisions the target where one exists.
- R3. The document states the deployed connectivity posture, that an in-VPC workload reaches Bedrock with the direct provider and no hop, so a reader does not carry the laptop jump-host into a deployed mental model.

### Scope Boundaries

- The document is a mapping reference. It provisions no infrastructure and changes no runtime code.
- The FedRAMP control mapping is a separate roadmap feature with its own plan and is not delivered here.
- The document does not duplicate each terraform module's own README. It links them.

### Resolved Decisions

- Resolved in planning. The mapping consolidates and links the v0.1.0 terraform module READMEs rather than restating their contents, so the modules stay the single source for their own detail.

### Sources

- Roadmap feature and verification table: [2026-09-02-1801-feat-remaining-work-roadmap-plan.md](2026-09-02-1801-feat-remaining-work-roadmap-plan.md).
- Local components: `compose.yaml`, `compose.override.yaml`, `scripts/config.py`.
- AWS targets already provisioned: `infra/terraform/vpc-endpoints`, `infra/terraform/ecr`, `infra/terraform/cloudtrail`, `infra/terraform/bedrock-iam`, `infra/terraform/bedrock-jumphost-fargate`, `infra/terraform/examples/private-bedrock`, each with a README.

## Planning Contract

### Key Technical Decisions

- KTD1. Structure the document as a table keyed by local component. Governs R1, R2. Rationale. A table lets a reader scan from a local service to its AWS target and the module that provisions it in one line.
- KTD2. Link the terraform module READMEs rather than restate them. Governs R2. Rationale. The modules already document themselves, and a copy would drift.
- KTD3. State the deployed connectivity posture explicitly. Governs R3. Rationale. The jump-host is a laptop path, and a deployed workload uses the direct provider, so the document must prevent that confusion.

### High-Level Design

The document opens with the deployment posture, that the platform runs in-VPC in a deployed environment and reaches Bedrock through the direct provider. A table then maps each local component to its AWS target and the provisioning module. A closing section lists the components that have no terraform yet, so the gaps are visible.

### Assumptions

- The v0.1.0 terraform modules are the authoritative description of the AWS targets they provision.

### Sequencing

A single unit writes the document. It has no code dependency.

## Implementation Units

### U1. Write the local-to-AWS mapping document

Goal. Commit a document that maps each local component to its AWS target and provisioning module and states the deployed connectivity posture.
Requirements. R1, R2, R3.
Files. `docs/local-to-aws-mapping.md` (new).
Approach. Enumerate the local components from the compose stack and the config surface. For each, name the AWS target and link the terraform module that provisions it where one exists. State that a deployed workload reaches Bedrock with the direct provider. List the components with no terraform yet in a gaps section.
Test scenarios. A reader finds every compose service and object store in the table with a named AWS target. Each row that has a provisioning module links it. The deployed connectivity posture is stated. The gaps section names components with no terraform.
Verification. Document review against the compose stack and the terraform module list.
