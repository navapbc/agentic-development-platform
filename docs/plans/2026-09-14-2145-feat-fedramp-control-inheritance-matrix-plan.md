---
title: FedRAMP Control Inheritance and Shared-Responsibility Matrix - Plan
type: feat
date: 2026-09-14
origin: docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-brainstorm
execution: docs
---

# FedRAMP Control Inheritance and Shared-Responsibility Matrix - Plan

## Goal Capsule

Objective. A committed matrix records, per FedRAMP Medium control family, which controls the platform inherits from the AWS authorization, which the platform implements, and which the deploying organization owns, so a reader knows exactly what the platform must build to conform.

Means. Write one matrix document under `docs/` keyed by control family, grounded in the AWS FedRAMP Medium authorization the hosting account holds and in the controls the platform implements, and cite the implementing terraform or code for each platform-owned control.

Product authority. This plan enriches the FedRAMP readiness scope of the roadmap at [2026-09-02-1801-feat-remaining-work-roadmap-plan.md](2026-09-02-1801-feat-remaining-work-roadmap-plan.md). It scopes the platform-owned control set that the other FedRAMP features implement.

Stop conditions. The matrix is committed and every FedRAMP Medium control family has an inherited, platform-implemented, or organization-owned designation with a citation for each platform-owned control.

## Product Contract

### Summary

The hosting account holds a FedRAMP Medium authorization, so the platform inherits the controls AWS owns and implements the controls it configures on top. Without a committed matrix the split is implicit, so a reader cannot tell which controls the platform must build. This matrix records the split per control family, cites the implementing terraform or code for each platform-owned control, and names the organization-owned operational controls with the enablement the platform provides. It is the scoping artifact the other FedRAMP features derive from.

### Problem Frame

FedRAMP conformance for a hosted system is a shared responsibility. AWS provides the physical, environmental, hypervisor, and cryptographic-module controls under its authorization, the platform provides the configuration and provisioning controls, and the deploying organization provides the operational and program controls. A matrix that states this split per family turns an open-ended readiness question into a bounded platform-owned control set.

### Requirements

- R1. A committed matrix under `docs/` covers the FedRAMP Medium control families.
- R2. Each family designates the inherited, platform-implemented, and organization-owned controls.
- R3. Each platform-owned control cites the terraform or code that implements it, or the feature that will.
- R4. The organization-owned operational controls name the enablement the platform provides.

### Scope Boundaries

- The matrix is a reference document. It provisions nothing and changes no runtime code.
- The matrix records inheritance and responsibility. It does not author a full System Security Plan.
- The platform-owned controls it names are implemented by their own dedicated features.

### Sources

- Roadmap: [2026-09-02-1801-feat-remaining-work-roadmap-plan.md](2026-09-02-1801-feat-remaining-work-roadmap-plan.md).
- Hardening design: [fedramp-medium-hardening-design.md](../fedramp-medium-hardening-design.md).
- Local-to-AWS mapping: [local-to-aws-mapping.md](../local-to-aws-mapping.md).
- Implemented controls: the modules under `infra/terraform`, the signing docs, and the evidence and audit paths.

## Planning Contract

### Key Technical Decisions

- KTD1. Key the matrix by control family. Governs R1, R2. Rationale. An assessor and an engineer both reason per family, so the family is the row.
- KTD2. Cite the implementation for every platform-owned control. Governs R3. Rationale. A responsibility claim without a citation is unverifiable.
- KTD3. Record enablement, not ownership, for organization controls. Governs R4. Rationale. The platform cannot own an operational process, but it can provide the data and hooks the process needs.

### High-Level Design

The matrix opens with the inheritance model and the hosting account authorization. A table follows with one row per control family and columns for inherited, platform-implemented, and organization-owned, with a citation in the platform column. A closing section lists the platform-owned controls that map to a dedicated implementing feature, so the readiness backlog is explicit.

### Sequencing

A single unit writes the matrix. It is the scoping artifact for the other FedRAMP features and reads best before them.

## Implementation Units

### U1. Write the control inheritance and shared-responsibility matrix

Goal. Commit the matrix that designates inherited, platform-implemented, and organization-owned controls per family with citations.
Requirements. R1, R2, R3, R4.
Files. `docs/fedramp-control-inheritance.md` (new), and a link back from `docs/fedramp-medium-hardening-design.md`.
Approach. Enumerate the FedRAMP Medium control families. For each, designate the inherited, platform-implemented, and organization-owned controls, citing the terraform or code for platform-owned controls or the feature that will implement them. Name the enablement for organization-owned operational controls. Link the matrix from the hardening design.
Test scenarios. Every family has a designation. Each platform-owned control cites an implementation or a feature. Each organization-owned control names the platform enablement.
Verification. Document review against the terraform modules, the signing docs, and the hardening design.
