---
title: Observability Terraform - Plan
type: feat
date: 2026-09-14
origin: docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-brainstorm
execution: infra
---

# Observability Terraform - Plan

## Goal Capsule

Objective. The platform's metrics, traces, and dashboards run on managed AWS observability services with encryption, scoped access, and private ingestion, so the observability stack conforms to the platform-owned FedRAMP Medium controls and supports the continuous-monitoring enablement the platform provides.

Means. Add toggleable terraform modules for Amazon Managed Service for Prometheus, Amazon Managed Grafana, and a managed trace destination, each with encryption, scoped IAM access, and private ingestion where supported.

Product authority. This plan enriches the FedRAMP readiness scope of the roadmap at [2026-09-02-1801-feat-remaining-work-roadmap-plan.md](2026-09-02-1801-feat-remaining-work-roadmap-plan.md). It provisions the observability targets the local-to-AWS mapping names and supports the CA-7 enablement the inheritance matrix records.

Stop conditions. Managed Prometheus, Managed Grafana, and the trace destination are provisioned with encryption and scoped access, and `terraform validate` passes. The modules are toggleable.

## Product Contract

### Summary

Prometheus, Grafana, Tempo, and the OpenTelemetry collector run in compose locally, and the local-to-AWS mapping records that they map to managed AWS observability services in a deployed environment, but no terraform provisions them. This plan provisions Amazon Managed Service for Prometheus for metrics, Amazon Managed Grafana for dashboards, and a managed trace destination, each with encryption, scoped IAM access, and private ingestion where the service supports it. It also provisions the Langfuse LLM-tracing stack and its analytics store, or connects Langfuse Cloud, since Langfuse is the platform's LLM observability. The observability stack supports the continuous-monitoring enablement the platform provides toward CA-7.

### Problem Frame

Observability is the substrate for continuous monitoring, which FedRAMP Medium expects under CA-7, and it holds operational telemetry that must be encrypted and access-scoped. AWS provides Managed Prometheus and Managed Grafana as managed services carrying the inherited controls, so the platform's responsibility is the terraform that provisions them with encryption, scoped access, and private ingestion, and wires the collector to them. Langfuse and its analytics store are the platform's LLM-trace tier and are provisioned with the same encryption and isolation posture.

### Requirements

- R1. A terraform module provisions Amazon Managed Service for Prometheus with scoped IAM ingestion and query access.
- R2. A terraform module provisions Amazon Managed Grafana with encryption, scoped access, and a data source to the metrics workspace.
- R3. A managed trace destination is provisioned or configured for the OpenTelemetry collector to export to.
- R4. Ingestion uses private connectivity where the service supports it.
- R5. The Langfuse stack and its analytics store are provisioned on the container platform with encryption at rest and private networking, or Langfuse Cloud is connected, and the analytics store is encrypted and isolated.
- R6. The modules are toggleable so an account that runs observability elsewhere provisions nothing.

### Scope Boundaries

- These modules provision the managed observability services and the Langfuse stack and its analytics store. The collector that exports to them is a platform service provisioned by the container-platform services feature.
- Dashboards and alert rules content are out of scope. This plan provisions the platform and its access, not the alert definitions.
- The modules follow the toggleable, reusable pattern of the existing terraform modules.

### Sources

- Roadmap and mapping: [2026-09-02-1801-feat-remaining-work-roadmap-plan.md](2026-09-02-1801-feat-remaining-work-roadmap-plan.md), [local-to-aws-mapping.md](../local-to-aws-mapping.md).
- Observability surface: `compose.yaml` (prometheus, grafana, tempo, otel-collector), `scripts/config.py` (`otel_exporter_otlp_endpoint`).
- Module conventions: `infra/terraform/vpc-endpoints`, `infra/terraform/evidence-store`.

## Planning Contract

### Key Technical Decisions

- KTD1. Use Amazon Managed Service for Prometheus and Amazon Managed Grafana. Governs R1, R2. Rationale. The managed services carry the inherited AWS controls and expose the encryption and access settings the platform must configure.
- KTD2. Scope ingestion and query access by IAM. Governs R1, R2. Rationale. AC-6 requires the collector to ingest and the operators to query under distinct scoped roles.
- KTD3. Prefer private ingestion where supported. Governs R4. Rationale. SC-7 keeps telemetry off the public path where the service offers a private endpoint.

### High-Level Design

A `managed-prometheus` module provisions the workspace with an ingestion role and a query role. A `managed-grafana` module provisions the workspace with encryption, a scoped access role, and a data source pointed at the metrics workspace. A trace destination is provisioned or configured for the collector to export to. Ingestion uses the VPC endpoints where the service supports private connectivity. The modules are gated by create toggles and emit the workspace endpoints and role ARNs. The composition root gains optional wiring, and the collector configuration consumes the endpoints.

### Sequencing

U1 provisions Managed Prometheus. U2 provisions Managed Grafana and its data source. U3 provisions or configures the trace destination and wires the modules into the composition root. U2 depends on U1, U3 depends on U1 and U2.

## Implementation Units

### U1. Managed Prometheus module

Goal. Provision the metrics workspace with scoped ingestion and query access.
Requirements. R1, R4, R5.
Files. `infra/terraform/managed-prometheus/` (new module with versions, variables, main, outputs, README).
Approach. Provision the workspace, an ingestion role for the collector, and a query role for Grafana and operators. Use private ingestion through the VPC endpoints where supported. Gate on a create toggle. Emit the workspace endpoint and role ARNs.
Test scenarios. `terraform validate` passes. The workspace declares scoped ingestion and query roles. The module provisions nothing when disabled.
Verification. `terraform validate`, module review.

### U2. Managed Grafana module

Goal. Provision the dashboard workspace with encryption, scoped access, and a metrics data source.
Requirements. R2, R5.
Files. `infra/terraform/managed-grafana/` (new module with versions, variables, main, outputs, README).
Approach. Provision the workspace with encryption and a scoped access role, and configure a data source pointed at the metrics workspace query endpoint. Gate on a create toggle. Emit the workspace endpoint.
Test scenarios. `terraform validate` passes. The workspace declares encryption and a scoped access role and a data source to the metrics workspace. The module provisions nothing when disabled.
Verification. `terraform validate`, module review.

### U3. Trace destination and composition wiring

Goal. Provision or configure the trace destination and wire the observability modules into the composition root.
Requirements. R3, R4, R5.
Files. `infra/terraform/` trace destination resources, `infra/terraform/examples/private-bedrock/` and its README.
Approach. Provision or configure the managed trace destination the collector exports to, using private connectivity where supported. Add optional module blocks for the observability modules gated by their toggles, and surface the endpoints as outputs for the collector.
Test scenarios. `terraform validate` passes with observability enabled and disabled. The endpoints surface as outputs when enabled.
Verification. `terraform validate`, composition review.
