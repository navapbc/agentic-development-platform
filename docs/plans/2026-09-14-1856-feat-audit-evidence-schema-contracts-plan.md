---
title: Audit and Evidence Schema Contracts - Plan
type: feat
date: 2026-09-14
origin: docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-brainstorm
execution: code
---

# Audit and Evidence Schema Contracts - Plan

## Goal Capsule

Objective. Committed JSON-schema contracts define the audit-event and the evidence-object shapes, so a producer or an external consumer validates a record against a versioned contract rather than an implicit table definition.

Means. Derive two JSON schemas from the settled shapes already in code, commit them under `schemas/audit_events` and `schemas/evidence`, and add a validation check that the audit and evidence records the platform writes conform to the committed schemas.

Product authority. This plan enriches one feature from the roadmap at [2026-09-02-1801-feat-remaining-work-roadmap-plan.md](2026-09-02-1801-feat-remaining-work-roadmap-plan.md), the "Audit and evidence schema contracts" entry. It does not widen that feature's scope.

Stop conditions. Both schemas are committed and a test validates a representative record of each type against its schema. The contracts describe existing shapes, so no runtime write path changes.

## Product Contract

Product Contract preservation. Sourced from the roadmap feature "Audit and evidence schema contracts". The requirements below restate that feature's subtasks as R-IDs with no scope change.

### Summary

The `schemas/audit_events` and `schemas/evidence` directories hold only `.gitkeep`, so neither record type has a committed contract. The shapes are settled in code. The `audit_events` table and the `evidence_artifacts` table in `database/models/schema.py` define the fields, and `EvidenceRecord` in `services/review/evidence.py` defines the evidence write shape. This plan captures those shapes as versioned JSON schemas and validates that the records the platform writes conform.

### Problem Frame

Audit events and evidence objects are the platform's durable compliance record, but their shape lives only in the SQLAlchemy table definitions and a dataclass. An external consumer, an evidence exporter, or a future object-store sink has no contract to validate against. A committed JSON schema per type gives every producer and consumer a single versioned definition.

### Requirements

- R1. A committed JSON schema under `schemas/audit_events` defines the audit-event shape, keyed to the fields the `audit_events` table records.
- R2. A committed JSON schema under `schemas/evidence` defines the evidence-object shape, keyed to the fields the `evidence_artifacts` table and `EvidenceRecord` record.
- R3. Each schema carries a version identifier so a later shape change is a new schema version rather than a silent edit.
- R4. A test validates a representative audit event and a representative evidence record against their schemas.

### Scope Boundaries

- The contracts describe the existing shapes. This plan does not change the audit or evidence write paths.
- The object-store evidence sink and the retention configuration are separate roadmap features with their own plans.
- This plan does not add runtime schema validation to the hot write path. It adds a test-time conformance check.

### Resolved Decisions

- Resolved in planning. The schemas are derived from the committed table and dataclass shapes, so the contract matches what the platform writes today rather than proposing a new shape.
- Resolved in planning. The schema dialect is JSON Schema, matching the roadmap's "JSON-schema contracts" wording.

### Sources

- Roadmap feature and verification table: [2026-09-02-1801-feat-remaining-work-roadmap-plan.md](2026-09-02-1801-feat-remaining-work-roadmap-plan.md).
- Audit shape: `database/models/schema.py` `audit_events` table.
- Evidence shape: `database/models/schema.py` `evidence_artifacts` table and `services/review/evidence.py` `EvidenceRecord`.

## Planning Contract

### Key Technical Decisions

- KTD1. Derive the schemas from the settled table and dataclass shapes. Governs R1, R2. Rationale. The shapes exist and are exercised, so the contract must match them, not a hypothetical shape.
- KTD2. Version each schema with a `$id` and a version field. Governs R3. Rationale. A versioned contract lets a later shape change be additive rather than a breaking silent edit.
- KTD3. Enforce conformance in a test rather than on the write path. Governs R4. Rationale. The write path is hot and already typed, so a test-time check catches drift without a per-write cost.

### High-Level Design

Two JSON Schema files land under `schemas/audit_events` and `schemas/evidence`, each with a `$id`, a version, required fields, and field types drawn from the table columns and the dataclass. A unit test builds a representative record of each type, the same shape the producers write, and validates it against the committed schema with a JSON Schema validator. The test fails when a shape drifts from its contract.

### Assumptions

- A JSON Schema validator is available to the test suite or is a light dependency to add.

### Sequencing

U1 writes the audit schema. U2 writes the evidence schema. U3 adds the conformance test. U3 depends on U1 and U2.

## Implementation Units

### U1. Commit the audit-event JSON schema

Goal. Define and commit a versioned JSON schema for the audit-event shape.
Requirements. R1, R3.
Files. `schemas/audit_events/audit-event.schema.json` (new).
Approach. Map each `audit_events` column to a schema property with its type and nullability. Mark `event_id`, `task_id`, `execution_id`, and `event_type` required. Carry a `$id` and a version.
Test scenarios. The schema parses as valid JSON Schema. Required fields are present. Nullable columns are optional.
Verification. Covered by U3.

### U2. Commit the evidence-object JSON schema

Goal. Define and commit a versioned JSON schema for the evidence-object shape.
Requirements. R2, R3.
Files. `schemas/evidence/evidence-object.schema.json` (new).
Approach. Map each `evidence_artifacts` column and each `EvidenceRecord` field to a schema property. Mark `artifact_id`, `execution_id`, `content_hash`, and `source_component` required. Carry a `$id` and a version.
Test scenarios. The schema parses as valid JSON Schema. Required fields are present. The `retention` and `location` fields are optional.
Verification. Covered by U3.

### U3. Validate representative records against the schemas

Goal. Prove a representative record of each type conforms to its committed schema.
Requirements. R4.
Files. `tests/unit/test_schema_contracts.py` (new).
Approach. Build a representative audit event and a representative evidence record matching what the producers write, and validate each against its committed schema with a JSON Schema validator. Assert conformance and assert a malformed record fails validation.
Test scenarios. A representative audit event validates. A representative evidence record validates. A record missing a required field fails validation.
Verification. `tests/unit/test_schema_contracts.py`, `inv test`.
