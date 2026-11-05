---
title: FedRAMP Medium Hardening Design - Plan
type: feat
date: 2026-09-14
origin: docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-brainstorm
execution: docs
---

# FedRAMP Medium Hardening Design - Plan

## Goal Capsule

Objective. A committed hardening design ties each platform component to its FedRAMP Medium control families, so a reader can trace how the Temporal control plane, the edge endpoints, data, secrets, identity, images, audit export, and the webhook relay each meet the controls they are responsible for.

Means. Write one design document under `docs/` that names each component, states its hardening posture, and maps that posture to the FedRAMP Medium control families it satisfies, grounded in the controls the v0.1.0 terraform already implements.

Product authority. This plan enriches one feature from the roadmap at [2026-09-02-1801-feat-remaining-work-roadmap-plan.md](2026-09-02-1801-feat-remaining-work-roadmap-plan.md), the "FedRAMP Medium hardening design" entry. It does not widen that feature's scope.

Stop conditions. The design is committed and every named component maps to its control families. The design is a reference document, so it provisions nothing and changes no runtime code.

## Product Contract

Product Contract preservation. Sourced from the roadmap feature "FedRAMP Medium hardening design". The requirements below restate that feature's subtask as R-IDs with no scope change.

### Summary

The hardening design exists only as plan prose. Since the roadmap captured that state, v0.1.0 committed terraform that implements several of the controls the design will cite, the KMS-managed encryption, the least-privilege roles, the CloudTrail audit trail, the VPC endpoints, and the hardened buckets. This plan writes a committed design that ties each platform component to the FedRAMP Medium control families it satisfies, grounded in those implemented controls and naming the gaps where a control is not yet met.

### Problem Frame

A FedRAMP Medium authorization requires a documented mapping from each system component to the controls it satisfies. The platform has a Temporal control plane, edge endpoints, a data tier, secret material, identities, container images, an audit export path, and a webhook relay, and each carries a hardening posture that must be tied to a control family. Without a committed design, the posture is implicit and an assessor has nothing to trace.

### Requirements

- R1. A committed design under `docs/` covers the Temporal control plane, the edge endpoints, data, secrets, identity, images, audit export, and the webhook relay policy.
- R2. Each component maps to the FedRAMP Medium control families it satisfies, grounded in the controls the platform implements today.
- R3. The design names the gaps where a control family is not yet met, so the remaining hardening work is visible.

### Scope Boundaries

- The design is a reference document. It provisions no infrastructure and changes no runtime code.
- The design maps to FedRAMP Medium control families, not to a full System Security Plan control-by-control response.
- The local-to-AWS mapping is a separate roadmap feature with its own plan and is referenced rather than restated.

### Resolved Decisions

- Resolved in planning. The design maps to control families rather than authoring a full SSP, matching the roadmap's "hardening design" scope.
- Resolved in planning. The design grounds each posture in the controls the v0.1.0 terraform implements, so it describes the system as built and names gaps rather than describing an aspiration.

### Sources

- Roadmap feature and verification table: [2026-09-02-1801-feat-remaining-work-roadmap-plan.md](2026-09-02-1801-feat-remaining-work-roadmap-plan.md).
- Implemented controls: `infra/terraform/bedrock-iam`, `infra/terraform/vpc-endpoints`, `infra/terraform/cloudtrail`, `infra/terraform/ecr`, `infra/terraform/bedrock-jumphost-fargate`.
- Signing and audit posture: `docs/review-verdict-signing.md`, `docs/authored-identity-signing.md`.
- Local-to-AWS mapping: the companion roadmap feature plan.

## Planning Contract

### Key Technical Decisions

- KTD1. Organize the design by component, with a control-family mapping per component. Governs R1, R2. Rationale. An assessor traces from a component to the controls it satisfies, so the component is the primary key.
- KTD2. Ground each posture in an implemented control and cite the terraform or the code. Governs R2. Rationale. A design that cites the implementation is verifiable, and one that does not is an aspiration.
- KTD3. Name the gaps explicitly. Governs R3. Rationale. A hardening design that hides gaps is misleading, so the unmet control families are called out.

### High-Level Design

The document opens with the trust boundary and the deployment posture. A section per component follows, the Temporal control plane, the edge endpoints, the data tier, secrets, identity, images, the audit export, and the webhook relay policy. Each section states the hardening posture, cites the terraform or code that implements it, and maps it to the FedRAMP Medium control families it satisfies. A closing gaps section lists the control families not yet met and the work each needs.

### Assumptions

- The FedRAMP Medium control families are the baseline, consistent with the roadmap wording.
- The v0.1.0 terraform is the authoritative description of the implemented controls it provisions.

### Sequencing

A single unit writes the design. It has no code dependency and reads best after the local-to-AWS mapping exists.

## Implementation Units

### U1. Write the FedRAMP Medium hardening design

Goal. Commit a design that ties each component to its FedRAMP Medium control families, grounded in the implemented controls, and names the gaps.
Requirements. R1, R2, R3.
Files. `docs/fedramp-medium-hardening-design.md` (new).
Approach. Write a section per component covering the Temporal control plane, the edge endpoints, data, secrets, identity, images, audit export, and the webhook relay policy. For each, state the hardening posture, cite the terraform or code that implements it, and map it to the control families it satisfies. Close with a gaps section listing unmet control families and the work each needs.
Test scenarios. Every named component has a section with a control-family mapping. Each posture cites an implemented control. The gaps section lists the unmet control families.
Verification. Document review against the terraform modules and the signing and audit documentation.
