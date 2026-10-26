---
title: Full Evidence Artifact Coverage - Plan
type: feat
date: 2026-09-14
origin: docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-brainstorm
execution: code
---

# Full Evidence Artifact Coverage - Plan

## Goal Capsule

Objective. Every artifact type persists through the content-addressed evidence sink and is retrievable by id, so plans, pull-request summaries, approvals, prompt references, and build and test reports carry the same durable evidence trail that review verdicts, gate results, and scanner findings already carry.

Means. Route each unrouted artifact type through the evidence sink at the point it is produced, with a content hash and a source component, and confirm each is retrievable by id through the evidence reader.

Product authority. This plan enriches one feature from the roadmap at [2026-09-02-1801-feat-remaining-work-roadmap-plan.md](2026-09-02-1801-feat-remaining-work-roadmap-plan.md), the "Full evidence artifact coverage" entry. It does not widen that feature's scope.

Stop conditions. Plans, pull-request summaries, approvals, prompt references, and build and test reports each persist through the evidence sink and are retrievable by id. No new artifact type is invented.

## Product Contract

Product Contract preservation. Sourced from the roadmap feature "Full evidence artifact coverage". The requirements below restate that feature's subtasks as R-IDs with no scope change.

### Summary

The evidence sink stores review verdicts, gate results, and scanner findings today. `services/review/gate.py`, `services/orchestration/temporal_activities/gates.py`, and `services/orchestration/temporal_activities/security.py` build evidence records for those types, and the reader resolves them by id. Plans, pull-request summaries, approvals, prompt references, and build and test reports are produced in the workflow but are not routed through the sink, so they carry no evidence record. This plan routes each of those types through the content-addressed sink and confirms each is retrievable by id.

### Problem Frame

An evidence trail is complete only when every artifact that a run produces is content-addressed and retrievable. The workflow produces a plan at plan acceptance, a pull-request summary at promotion, an approval at the approval gate, a prompt reference at each model call, and a build and test report from CI. None of these lands in the evidence sink, so the trail has holes. Routing each through the sink at its production point, with a content hash and a source component, closes the holes.

### Requirements

- R1. Plans persist through the content-addressed evidence sink at plan acceptance.
- R2. Pull-request summaries persist through the sink at promotion.
- R3. Approvals persist through the sink at the approval gate.
- R4. Prompt references persist through the sink where a model call records its prompt.
- R5. Build and test reports persist through the sink when CI reports.
- R6. Each newly routed artifact is retrievable by id through the evidence reader.

### Scope Boundaries

- This plan routes existing artifact types through the sink. It does not invent new artifact types or change how the artifacts are produced.
- The object-store sink and the retention configuration are separate roadmap features. This plan routes to the sink the platform provides.
- This plan does not backfill evidence for past runs.

### Resolved Decisions

- Resolved in planning. Each artifact is routed at its production point with a source component that names the artifact type, matching the pattern the gate and scanner producers already use.
- Resolved in planning. The content hash is computed over the canonical artifact body so a duplicate write is idempotent on the content-addressed key.

### Sources

- Roadmap feature and verification table: [2026-09-02-1801-feat-remaining-work-roadmap-plan.md](2026-09-02-1801-feat-remaining-work-roadmap-plan.md).
- Evidence sink and reader: `services/review/evidence.py`, `services/integration/mcp_gateway/evidence_reader.py`.
- Producers already routed: `services/review/gate.py`, `services/orchestration/temporal_activities/gates.py`, `services/orchestration/temporal_activities/security.py`.
- Production points to route: the plan-accept, promotion, approval, model-call, and CI-report paths in `services/orchestration/temporal_workflows/draft_pull_request.py` and its activities.

## Planning Contract

### Key Technical Decisions

- KTD1. Route each artifact at its production point, not in a batch pass. Governs R1 through R5. Rationale. The production point has the artifact body and the execution context, so the record is built where the artifact exists.
- KTD2. Reuse the existing evidence record build and source-component pattern. Governs R1 through R5. Rationale. The gate and scanner producers already establish the pattern, so the new producers follow it rather than inventing a new one.
- KTD3. Confirm retrieval by id for each new type through the reader. Governs R6. Rationale. Coverage is only real when the reader returns the artifact, so each type is confirmed retrievable.

### High-Level Design

At plan acceptance, the workflow builds an evidence record for the plan and persists it. At promotion, it does the same for the pull-request summary. At the approval gate, it records the approval. Where a model call records its prompt, it records a prompt reference. When CI reports, it records the build and test report. Each record carries a content hash over the artifact body and a source component that names the type. A reader test resolves each new type by id.

### Assumptions

- Each artifact has a canonical serialization to hash so the content address is stable.
- The evidence sink write is available from each production point in the workflow or its activities.

### Sequencing

U1 routes plans and pull-request summaries. U2 routes approvals and prompt references. U3 routes build and test reports. U4 confirms retrieval by id for each new type. U4 depends on U1 through U3.

## Implementation Units

### U1. Route plans and pull-request summaries

Goal. Persist plans at plan acceptance and pull-request summaries at promotion through the evidence sink.
Requirements. R1, R2.
Files. `services/orchestration/temporal_workflows/draft_pull_request.py` and its plan-accept and promotion activities, `services/review/evidence.py`.
Approach. At plan acceptance, build an evidence record over the canonical plan body with a plan source component and persist it. At promotion, do the same for the pull-request summary.
Test scenarios. An accepted plan writes an evidence record with a plan source component. A promotion writes an evidence record for the pull-request summary. A duplicate write is idempotent on the content hash.
Verification. `tests/unit/test_draft_pull_request_workflow.py` additions, `inv test`.

### U2. Route approvals and prompt references

Goal. Persist approvals at the approval gate and prompt references where a model call records its prompt.
Requirements. R3, R4.
Files. the approval-gate path and the model-call path in `services/orchestration/temporal_activities/`, `services/review/evidence.py`.
Approach. At the approval gate, build an evidence record over the approval decision with an approval source component. Where a model call records its prompt, build a record over the prompt reference with a prompt source component.
Test scenarios. An approval writes an evidence record with an approval source component. A model call writes a prompt-reference evidence record. Both are idempotent on the content hash.
Verification. the relevant activity tests, `inv test`.

### U3. Route build and test reports

Goal. Persist the build and test report through the evidence sink when CI reports.
Requirements. R5.
Files. the CI-report path in `services/orchestration/temporal_activities/`, `services/review/evidence.py`.
Approach. When CI reports, build an evidence record over the report body with a build-and-test source component and persist it.
Test scenarios. A CI report writes an evidence record with a build-and-test source component. A duplicate report is idempotent on the content hash.
Verification. the CI activity test, `inv test`.

### U4. Confirm retrieval by id for each new type

Goal. Confirm each newly routed artifact is retrievable by id through the evidence reader.
Requirements. R6.
Files. `services/integration/mcp_gateway/evidence_reader.py`, a reader test.
Approach. For a plan, a pull-request summary, an approval, a prompt reference, and a build and test report, persist a record and resolve it by id through the reader, asserting the returned artifact matches.
Test scenarios. Each new artifact type is retrievable by id. A missing id returns a clear not-found result.
Verification. `tests/unit/test_evidence_reader.py` or the reader test, `inv test`.
