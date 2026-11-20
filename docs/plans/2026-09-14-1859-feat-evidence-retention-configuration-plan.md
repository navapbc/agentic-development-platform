---
title: Evidence Retention Configuration - Plan
type: feat
date: 2026-09-14
origin: docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-brainstorm
execution: code
---

# Evidence Retention Configuration - Plan

## Goal Capsule

Objective. A configuration value sets the evidence retention period, defaulting to the FedRAMP Moderate baseline, and every evidence record carries that retention, so the retention window is a governed setting rather than an unset column.

Means. Add a retention configuration value to the settings surface with a FedRAMP Moderate default, and wire it into the evidence records the sink writes.

Product authority. This plan enriches one feature from the roadmap at [2026-09-02-1801-feat-remaining-work-roadmap-plan.md](2026-09-02-1801-feat-remaining-work-roadmap-plan.md), the "Evidence retention configuration" entry. It does not widen that feature's scope.

Stop conditions. The configuration value exists with the baseline default and every evidence record the platform writes carries the configured retention. No object-store enforcement is built here.

## Product Contract

Product Contract preservation. Sourced from the roadmap feature "Evidence retention configuration". The requirements below restate that feature's subtask as R-IDs with no scope change.

### Summary

The `evidence_artifacts` table and `EvidenceRecord` carry a `retention` field, but no configuration value sets it, so the column is written as null today. This plan adds a retention configuration value with a FedRAMP Moderate default and wires it into the evidence records at the point they are built, so every record carries the configured retention.

### Problem Frame

Retention is the input the WORM store enforces, and the compliance baseline governs how long an evidence object is immutable. The retention field exists on the record but nothing sets it, so a downstream Object Lock write would have no window to apply. A single configuration value with a defensible default, wired into the record build, makes retention a governed setting.

### Requirements

- R1. A configuration value sets the evidence retention period.
- R2. The configuration value defaults to the FedRAMP Moderate baseline.
- R3. The evidence records the platform writes carry the configured retention.

### Scope Boundaries

- This plan sets and carries the retention value. Enforcing it with Object Lock is the S3-backed WORM store feature with its own plan.
- This plan does not add per-classification retention tiers. It sets one baseline retention value.

### Resolved Decisions

- Resolved in planning. The default is the FedRAMP Moderate baseline, matching the roadmap wording, and a deployment overrides it through the configuration value.

### Sources

- Roadmap feature and verification table: [2026-09-02-1801-feat-remaining-work-roadmap-plan.md](2026-09-02-1801-feat-remaining-work-roadmap-plan.md).
- Evidence record and sink: `services/review/evidence.py`, `database/models/schema.py` `evidence_artifacts`.
- Configuration surface: `scripts/config.py`, `.env.example`.
- Evidence producers that build records: `services/review/gate.py`, `services/orchestration/temporal_activities/gates.py`, `services/orchestration/temporal_activities/security.py`.

## Planning Contract

### Key Technical Decisions

- KTD1. Add one retention setting with a baseline default. Governs R1, R2. Rationale. Retention is one governed window, so one setting with a defensible default is enough.
- KTD2. Apply the retention where the record is built, not at the column default. Governs R3. Rationale. The record build is the single point every producer passes through, so applying it there keeps the value consistent and testable.

### High-Level Design

A retention setting lands in `scripts/config.py` with a FedRAMP Moderate default and an `.env.example` entry. The evidence record build reads the setting and sets the record's retention. A unit test asserts a built record carries the configured retention and that an override changes it.

### Assumptions

- The retention value is expressed in a unit the WORM store consumes when it enforces Object Lock.

### Sequencing

U1 adds the setting. U2 wires it into the record build. U2 depends on U1.

## Implementation Units

### U1. Add the retention configuration value

Goal. Add a retention setting with the FedRAMP Moderate baseline default.
Requirements. R1, R2.
Files. `scripts/config.py`, `.env.example`.
Approach. Add a retention setting to the settings model with the baseline default and an `.env.example` entry. Express the value in the unit the WORM store consumes.
Test scenarios. The default resolves to the baseline. An environment override changes the value.
Verification. `tests/unit/test_config.py` or the settings test, `inv test`.

### U2. Wire retention into evidence records

Goal. Set the configured retention on every evidence record the platform writes.
Requirements. R3.
Files. `services/review/evidence.py` and the record-build path shared by `services/review/gate.py`, `services/orchestration/temporal_activities/gates.py`, and `services/orchestration/temporal_activities/security.py`.
Approach. Read the retention setting where the evidence record is built and set the record's retention. Confirm every producer passes through that build so no record is written without the retention.
Test scenarios. A built evidence record carries the configured retention. An override changes the retention on the record. No producer writes a record with a null retention.
Verification. `tests/unit/test_evidence.py` or the evidence test, `inv test`.
