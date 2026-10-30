---
title: Temporal Control Plane Terraform - Plan
type: feat
date: 2026-09-14
origin: docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-brainstorm
execution: infra
---

# Temporal Control Plane Terraform - Plan

## Goal Capsule

Objective. The Temporal control plane the workers depend on is provisioned in the target VPC with TLS, private networking, and a scoped access path, or connected to a FedRAMP-eligible managed Temporal offering, so the control plane conforms to the platform-owned FedRAMP Medium controls.

Means. Add a toggleable terraform module that provisions a self-hosted Temporal control plane on the container platform with TLS and private networking, or wires a managed Temporal connection with mutual TLS, and exposes the endpoint to the workers.

Product authority. This plan enriches the FedRAMP readiness scope of the roadmap at [2026-09-02-1801-feat-remaining-work-roadmap-plan.md](2026-09-02-1801-feat-remaining-work-roadmap-plan.md). It provisions the Temporal control plane target the local-to-AWS mapping names.

Stop conditions. The control plane is provisioned in the VPC with TLS and private networking, or a managed connection with mutual TLS is configured, and `terraform validate` passes. The module is toggleable.

## Product Contract

### Summary

Temporal runs in compose locally, and the local-to-AWS mapping records that the control plane runs on the container platform or a managed offering in a deployed environment, but no terraform provisions it. This plan provisions a self-hosted Temporal control plane on the container platform with TLS in transit, private subnet placement, a scoped access path, and its persistence on the managed data tier, or, when a managed Temporal offering is selected, configures the connection with mutual TLS and a scoped namespace. Either path exposes the endpoint the workers connect to.

### Problem Frame

The control plane schedules and stores workflow state, so it is a sensitive component. FedRAMP Medium expects SC-7 network isolation, SC-8 TLS, AC-3 scoped access, and CP durability for it. A self-hosted control plane inherits the AWS controls of the container platform and the data tier and requires the platform to configure TLS, isolation, and persistence. A managed offering shifts more control inheritance to the provider and requires the platform to configure a mutually authenticated connection.

### Requirements

- R1. A terraform module provisions the Temporal control plane, defaulting to a self-hosted deployment on the container platform.
- R2. The self-hosted path enforces TLS in transit, private subnet placement, and a security group that admits the frontend port only from the worker security group.
- R3. The self-hosted path persists state on the managed data tier rather than ephemeral storage.
- R4. A managed-offering path is supported through configuration that establishes a mutually authenticated TLS connection and a scoped namespace.
- R5. The module exposes the Temporal endpoint for the workers, and is toggleable so an account that runs Temporal elsewhere provisions nothing.

### Scope Boundaries

- This module provisions or connects the control plane. The workers that connect to it are provisioned by the container-platform services feature.
- The persistence store is provisioned by the data tier feature and consumed here by reference.
- This plan does not build a custom Temporal distribution. It deploys the standard server or connects a managed one.

### Sources

- Roadmap and mapping: [2026-09-02-1801-feat-remaining-work-roadmap-plan.md](2026-09-02-1801-feat-remaining-work-roadmap-plan.md), [local-to-aws-mapping.md](../local-to-aws-mapping.md).
- Worker connection: `services/orchestration/worker.py`, `scripts/config.py` (`temporal_host`, `temporal_namespace`).
- Compute and data tier: the container-platform services and data-tier terraform features.

## Planning Contract

### Key Technical Decisions

- KTD1. Default to a self-hosted control plane on the container platform. Governs R1, R2, R3. Rationale. It reuses the same VPC, data tier, and inherited AWS controls the rest of the platform uses, so the posture is consistent.
- KTD2. Persist state on the managed data tier. Governs R3. Rationale. Ephemeral persistence loses workflow state, and the managed data tier already carries the encryption and backup controls.
- KTD3. Support a managed offering through configuration. Governs R4. Rationale. A deployment may prefer a managed Temporal, so the module offers a mutually authenticated connection path without forcing self-hosting.

### High-Level Design

A `temporal-control-plane` module has two modes. In the self-hosted mode it provisions the Temporal server on the container platform in private subnets with TLS, a security group admitting the frontend port only from the worker security group, and persistence pointed at the managed data tier. In the managed mode it configures a connection with mutual TLS and a scoped namespace, provisioning the client certificates or references the workers use. The module emits the endpoint and is gated by a create toggle. The composition root gains optional wiring, and the worker configuration consumes the endpoint.

### Sequencing

U1 provisions the self-hosted control plane. U2 adds the managed-connection mode. U3 wires the module into the composition root. The modes are independent, and U3 depends on U1 and U2.

## Implementation Units

### U1. Self-hosted Temporal control plane

Goal. Provision the control plane on the container platform with TLS, isolation, and managed persistence.
Requirements. R1, R2, R3, R5.
Files. `infra/terraform/temporal-control-plane/` (new module with versions, variables, main, outputs, README).
Approach. Provision the Temporal server on the container platform in private subnets, enforce TLS, scope the security group to the worker source on the frontend port, and point persistence at the managed data tier. Gate on a create toggle. Emit the endpoint.
Test scenarios. `terraform validate` passes. The server runs in private subnets with TLS. The security group admits only the worker source. Persistence targets the managed data tier. The module provisions nothing when disabled.
Verification. `terraform validate`, module review.

### U2. Managed Temporal connection mode

Goal. Support connecting to a managed Temporal offering with mutual TLS and a scoped namespace.
Requirements. R4, R5.
Files. `infra/terraform/temporal-control-plane/` (managed-mode variables and resources), README.
Approach. Add a mode that, instead of provisioning a server, configures the mutually authenticated connection and the scoped namespace the workers use, provisioning the client certificate references. Emit the endpoint.
Test scenarios. `terraform validate` passes in managed mode. The connection declares mutual TLS and a scoped namespace. The endpoint surfaces.
Verification. `terraform validate`, module review.

### U3. Wire the control plane into the composition root

Goal. Make the composition root provision or connect the control plane optionally.
Requirements. R5.
Files. `infra/terraform/examples/private-bedrock/` and its README.
Approach. Add an optional temporal-control-plane module block gated by its toggle and mode, passing the private subnets, the worker security group, and the data tier endpoint, and surface the Temporal endpoint as an output for the workers.
Test scenarios. `terraform validate` passes with the control plane enabled, disabled, and in each mode. The endpoint surfaces when enabled.
Verification. `terraform validate`, composition review.
