---
title: Private-endpoint Jump-host Residual Hardening - Plan
type: feat
date: 2026-09-14
origin: docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-brainstorm
execution: code
---

# Private-endpoint Jump-host Residual Hardening - Plan

## Goal Capsule

Objective. The jump-host container image and its provisioning meet the image supply-chain and least-privilege posture a FedRAMP Medium environment expects, so the delivered laptop-to-VPC Bedrock path is hardened rather than only functional.

Means. Pin the jump-host base image by digest, run the container as a non-root user, confirm the ECR repository scans the image on push, review and tighten the Fargate task and execution roles to the minimum the ECS Exec forward needs, and record the hardened posture.

Product authority. This plan enriches one feature from the roadmap at [2026-09-02-1801-feat-remaining-work-roadmap-plan.md](2026-09-02-1801-feat-remaining-work-roadmap-plan.md), the "Private-endpoint jump-host container image" entry. The image, the Fargate module, and the connectivity provider that satisfy the original subtasks are already delivered in v0.1.0, so this plan covers only the residual hardening and does not widen the feature's scope.

Stop conditions. The image is digest-pinned and non-root, the repository scans on push, the task and execution roles are confirmed least-privilege, and the posture is recorded. No new connectivity path is built.

## Product Contract

Product Contract preservation. Sourced from the roadmap feature "Private-endpoint jump-host container image". The original subtasks, building a deployable jump-host image and wiring a connectivity provider through it, are delivered in v0.1.0. This plan restates the residual hardening as R-IDs and does not add new feature scope.

### Summary

v0.1.0 delivered the jump-host as `docker/jumphost-base`, the `infra/terraform/bedrock-jumphost-fargate` module, the `JumphostConnectivity` provider in `scripts/connectivity_providers.py`, and `infra/bin/connect_to_jumphost.sh`, which together terminate the developer forward inside the target VPC and forward a laptop to the private Bedrock endpoint. The functional feature is complete. The residual gap is supply-chain and least-privilege hardening. The base image is pinned only to a floating tag through a build argument, the container runs as root with no non-root user, and the task and execution roles warrant a least-privilege confirmation. This plan closes those gaps.

### Problem Frame

The jump-host image is minimal and short-lived, but a FedRAMP Medium posture expects a pinned image provenance, a non-root runtime, image scanning, and least-privilege roles. The `docker/jumphost-base` Dockerfile sets `BASE_IMAGE` to a floating Alpine tag and defines no `USER`, so a rebuild can drift and the process runs as root. The ECR module supports `scan_on_push`, so the residual work confirms it is enabled for this repository. The Fargate task role grants only the `ssmmessages` channel actions the ECS Exec forward needs, which this plan confirms and documents.

### Requirements

- R1. The jump-host base image is pinned by digest so a rebuild produces the same base.
- R2. The jump-host container runs as a non-root user.
- R3. The ECR repository that holds the jump-host image scans the image on push.
- R4. The Fargate task and execution roles are confirmed to grant only the permissions the ECS Exec forward and image pull need, and the confirmation is recorded.
- R5. The hardened posture is recorded so a reader knows the image provenance, the runtime user, the scan setting, and the role scope.

### Scope Boundaries

- This plan hardens the delivered jump-host. It builds no new connectivity path and changes no provider behavior.
- The deployed in-VPC path uses the direct provider with no hop and is unaffected.
- The broader FedRAMP control mapping is a separate roadmap feature that references this posture.

### Resolved Decisions

- Resolved in planning. The functional feature is delivered, so this plan is residual hardening only, per the decision to capture remaining hardening rather than rebuild delivered work.
- Resolved in planning. The image stays a minimal Alpine base, pinned by digest and run as non-root, rather than switching base images.

### Sources

- Roadmap feature and verification table: [2026-09-02-1801-feat-remaining-work-roadmap-plan.md](2026-09-02-1801-feat-remaining-work-roadmap-plan.md).
- Delivered image and entrypoint: `docker/jumphost-base/Dockerfile`, `docker/jumphost-base/entrypoint.sh`.
- Delivered module and roles: `infra/terraform/bedrock-jumphost-fargate` (`iam.tf` task and execution roles), its README.
- Delivered provider and forward: `scripts/connectivity_providers.py` `JumphostConnectivity`, `infra/bin/connect_to_jumphost.sh`, `infra/bin/publish_jumphost_image.sh`.
- Repository scanning: `infra/terraform/ecr` (`scan_on_push`).

## Planning Contract

### Key Technical Decisions

- KTD1. Pin the base image by digest through the build argument default. Governs R1. Rationale. A digest pin makes a rebuild reproducible, and a floating tag does not.
- KTD2. Add a non-root user to the image. Governs R2. Rationale. The entrypoint only sleeps and holds the channel, so it needs no root, and a non-root runtime reduces the blast radius.
- KTD3. Confirm and, if needed, enable scan-on-push for the jump-host repository. Governs R3. Rationale. The ECR module already supports the setting, so the residual work is confirming it applies to this repository.
- KTD4. Review the task and execution roles and record the least-privilege confirmation. Governs R4, R5. Rationale. The roles look minimal, so the residual work is a confirmation and a recorded rationale, not a rewrite.

### High-Level Design

The Dockerfile pins the base image by digest and adds a non-root user that the entrypoint runs as. The publish path is unchanged. A terraform confirmation ensures the jump-host repository is created with scan-on-push. A short review of the task and execution roles confirms they grant only the ECS Exec channel actions and the image pull, and the module README records the image provenance, the runtime user, the scan setting, and the role scope.

### Assumptions

- The Alpine base has a resolvable digest for the pinned version and architecture the publish script builds.
- The ECR module is the repository source for the jump-host image.

### Sequencing

U1 hardens the image. U2 confirms scan-on-push. U3 confirms the roles and records the posture. The units are independent and can proceed in any order.

## Implementation Units

### U1. Pin and de-root the jump-host image

Goal. Pin the base image by digest and run the container as a non-root user.
Requirements. R1, R2.
Files. `docker/jumphost-base/Dockerfile`, `docker/jumphost-base/entrypoint.sh` if a permission adjustment is needed.
Approach. Set the base-image build argument default to a digest-pinned reference. Add a non-root user and group and run the entrypoint as that user. Confirm the entrypoint still holds the channel for its TTL as a non-root user.
Test scenarios. A build resolves the pinned digest. The running container reports a non-root user. The entrypoint holds for its TTL under the non-root user.
Verification. `inv publish-jumphost-image` build path locally, image inspection.

### U2. Confirm scan-on-push for the jump-host repository

Goal. Ensure the ECR repository that holds the jump-host image scans on push.
Requirements. R3.
Files. the terraform that instantiates the ECR repository for the jump-host image, its variables.
Approach. Confirm the repository is created with `scan_on_push` enabled, setting the variable if it is not, and confirm the publish path pushes to that repository.
Test scenarios. `terraform validate` passes. The repository declares scan-on-push. The publish target matches the scanned repository.
Verification. `terraform validate`, module review.

### U3. Confirm least-privilege roles and record the posture

Goal. Confirm the Fargate task and execution roles are least-privilege and record the hardened posture.
Requirements. R4, R5.
Files. `infra/terraform/bedrock-jumphost-fargate/iam.tf` (review only unless a scope reduction is warranted), `infra/terraform/bedrock-jumphost-fargate/README.md`.
Approach. Review the task role for the `ssmmessages` channel actions and the execution role for the image pull and log write, reducing any grant broader than the ECS Exec forward needs. Record the image provenance, the runtime user, the scan setting, and the role scope in the README.
Test scenarios. The task role grants only the ECS Exec channel actions. The execution role grants only the image pull and log write. The README records the hardened posture.
Verification. Module review, `terraform validate`.
