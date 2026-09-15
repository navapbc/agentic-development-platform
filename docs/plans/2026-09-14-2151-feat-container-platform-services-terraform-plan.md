---
title: Container-Platform Services Terraform - Plan
type: feat
date: 2026-09-14
origin: docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-brainstorm
execution: infra
---

# Container-Platform Services Terraform - Plan

## Goal Capsule

Objective. The platform's worker and service containers run on a managed container platform with per-service task roles, secrets injected from the secret store, private networking, and log export to CloudWatch, so the compute layer conforms to the platform-owned FedRAMP Medium controls.

Means. Add a toggleable terraform module that provisions an ECS on Fargate service per platform container, each with a least-privilege task role, secret injection by ARN, placement in private subnets behind scoped security groups, and CloudWatch log export.

Product authority. This plan enriches the FedRAMP readiness scope of the roadmap at [2026-09-02-1801-feat-remaining-work-roadmap-plan.md](2026-09-02-1801-feat-remaining-work-roadmap-plan.md). It provisions the container-platform-services targets the local-to-AWS mapping names.

Stop conditions. Each platform service is provisioned with a scoped task role, secret injection, private networking, and log export, and `terraform validate` passes. The module is toggleable.

## Product Contract

### Summary

The workers and services run in compose locally, and the local-to-AWS mapping records that they run on a container platform in a deployed environment, but no terraform provisions them. This plan provisions an ECS on Fargate service per platform container, the workflow, activity, and review workers, the coordination service, the LangGraph service, LiteLLM, OPA, and the repository-intelligence services. Each service carries a least-privilege task role, injects its secrets from the secret store by ARN, runs in private subnets behind a scoped security group, and exports logs to CloudWatch.

### Problem Frame

The compute layer runs the platform code and holds the credentials that reach Bedrock, the data tier, and the secret store. FedRAMP Medium expects AC-6 least privilege per workload, SC-7 network isolation, SC-28 and SC-8 for the data in transit and at rest it handles, IA-5 secret handling, and AU log export. AWS provides these as ECS and IAM configuration, so the platform's responsibility is the terraform that provisions each service with the compliant settings.

### Requirements

- R1. A terraform module provisions an ECS on Fargate service per platform container.
- R2. Each service has a least-privilege task role scoped to the AWS actions that service needs.
- R3. Each service injects its secrets from the secret store by ARN rather than from plaintext.
- R4. Each service runs in private subnets behind a security group scoped to the ports it needs.
- R5. Each service exports its logs to a CloudWatch log group.
- R6. The module is toggleable so an account that runs the platform elsewhere provisions nothing.

### Scope Boundaries

- This module provisions the compute services. It consumes the registry, the data tier endpoints, the secret ARNs, and the endpoints from their own modules.
- This plan targets ECS on Fargate. A Kubernetes variant is out of scope.
- The Temporal control plane is a separate feature the workers connect to.

### Sources

- Roadmap and mapping: [2026-09-02-1801-feat-remaining-work-roadmap-plan.md](2026-09-02-1801-feat-remaining-work-roadmap-plan.md), [local-to-aws-mapping.md](../local-to-aws-mapping.md).
- Services and roles precedent: `compose.yaml`, `compose.override.yaml`, `infra/terraform/bedrock-jumphost-fargate` (Fargate task and role pattern), `infra/terraform/bedrock-iam`.
- Worker roles: `services/orchestration/worker.py` (`WORKER_ROLE_*`).

## Planning Contract

### Key Technical Decisions

- KTD1. Provision one Fargate service per platform container with its own task role. Governs R1, R2. Rationale. A per-service role is the unit of least privilege, so the review worker's role differs from the activity worker's.
- KTD2. Inject secrets by ARN from the secret store. Governs R3. Rationale. ECS resolves a secret ARN into the container environment without the value ever living in the task definition.
- KTD3. Place services in private subnets behind scoped security groups. Governs R4. Rationale. SC-7 requires the compute layer to be unreachable except on the ports each service needs.
- KTD4. Export logs to CloudWatch. Governs R5. Rationale. AU log export requires a durable log destination the account controls.

### High-Level Design

A `container-services` module takes the cluster, the private subnets, the registry image references, the data tier and cache endpoints, the secret ARNs, and the Temporal endpoint, and provisions a Fargate task definition and service per platform container. Each task definition declares a least-privilege task role, secret injection by ARN, a CloudWatch log configuration, and the environment the service needs. Security groups admit only the required ports, for example the review worker reaching the Temporal endpoint and the data tier. The module is gated by a create toggle. The composition root gains optional wiring that passes the outputs of the data tier, secret store, and registry modules.

### Sequencing

U1 defines the task roles and shared task-definition scaffolding. U2 provisions the services and their security groups. U3 wires the module into the composition root. U2 depends on U1, U3 depends on U2.

## Implementation Units

### U1. Per-service task roles and task-definition scaffolding

Goal. Define a least-privilege task role and a task-definition template per platform service.
Requirements. R2, R3, R5.
Files. `infra/terraform/container-services/` (new module, iam and task-definition files).
Approach. Define a task role per service scoped to the AWS actions it needs, for example Bedrock invoke for the review worker and secret read for the services that hold credentials. Build a task-definition template that injects secrets by ARN and configures CloudWatch logging.
Test scenarios. `terraform validate` passes. Each role grants only its service's actions. The task definition injects secrets by ARN and configures logging.
Verification. `terraform validate`, module review.

### U2. Fargate services and security groups

Goal. Provision a Fargate service per platform container in private subnets behind scoped security groups.
Requirements. R1, R4, R6.
Files. `infra/terraform/container-services/` (services and security-group files, variables, outputs, README).
Approach. Provision a Fargate service per container from the task definitions, placed in private subnets with a security group scoped to the ports each service needs. Gate on a create toggle. Emit the service names and security group ids.
Test scenarios. `terraform validate` passes. Each service runs in private subnets. Security groups admit only the required ports. The module provisions nothing when disabled.
Verification. `terraform validate`, module review.

### U3. Wire the compute layer into the composition root

Goal. Make the composition root provision the compute services optionally.
Requirements. R6.
Files. `infra/terraform/examples/private-bedrock/` and its README.
Approach. Add an optional container-services module block gated by its toggle, passing the cluster, private subnets, registry references, data tier and cache endpoints, secret ARNs, and Temporal endpoint.
Test scenarios. `terraform validate` passes with the compute layer enabled and disabled. The service outputs surface when enabled.
Verification. `terraform validate`, composition review.
