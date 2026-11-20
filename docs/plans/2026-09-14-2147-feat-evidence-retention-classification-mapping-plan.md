---
title: Evidence Retention Classification Mapping - Plan
type: feat
date: 2026-09-14
origin: docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-brainstorm
execution: code
---

# Evidence Retention Classification Mapping - Plan

## Goal Capsule

Objective. Each evidence record carries the retention its classification requires, so a classification maps to a defined retention period in days rather than every record taking one baseline window.

Means. Add a configuration mapping from record classification to retention days, resolve each record's retention from its classification at the record build, and fall back to the baseline when a classification is unmapped.

Product authority. This plan enriches the FedRAMP readiness scope of the roadmap at [2026-09-02-1801-feat-remaining-work-roadmap-plan.md](2026-09-02-1801-feat-remaining-work-roadmap-plan.md). It extends the evidence-retention configuration with a classification mapping the hardening design requires.

Stop conditions. A classification-to-retention mapping resolves each record's retention, the baseline applies to an unmapped classification, and the object store enforces the resolved window.

## Product Contract

### Summary

Evidence retention is a single configured window today. FedRAMP retention obligations differ by the sensitivity of the record, so a compliance evidence artifact and a routine gate result may warrant different retention periods. This plan adds a classification-to-retention mapping expressed in days, resolves each record's retention from its classification when the record is built, and falls back to the baseline window for an unmapped classification. The object store already enforces an integer-day retention, so the resolved value flows into the Object Lock window unchanged.

### Problem Frame

The retention configuration sets one baseline window that every record inherits. A record carries a classification, so a mapping from classification to retention days lets a deployment hold different record types for different periods, which the FedRAMP retention posture requires. The mapping resolves at the record build, the single point every producer passes through, and the resolved value is the integer-day retention the WORM store consumes.

### Requirements

- R1. A configuration mapping expresses a retention period in days per record classification.
- R2. The retention on each evidence record resolves from its classification through the mapping.
- R3. A record whose classification is not in the mapping falls back to the baseline retention.
- R4. The resolved retention flows into the object store Object Lock window unchanged.

### Scope Boundaries

- This plan sets and resolves retention by classification. Enforcing the window is the object store's responsibility, already delivered.
- Retention is expressed in days end to end, consistent with the object store.
- This plan does not reclassify existing records or backfill retention on past records.

### Sources

- Roadmap and hardening design: [2026-09-02-1801-feat-remaining-work-roadmap-plan.md](2026-09-02-1801-feat-remaining-work-roadmap-plan.md), [fedramp-medium-hardening-design.md](../fedramp-medium-hardening-design.md).
- Retention baseline and record build: `scripts/config.py`, `services/review/evidence.py`, the evidence producers in `services/review/gate.py` and `services/orchestration/temporal_activities/`.
- Retention consumption: `services/review/evidence.py` `ObjectStoreEvidenceSink._retention_days`.

## Planning Contract

### Key Technical Decisions

- KTD1. Express the mapping in days, keyed by classification. Governs R1, R4. Rationale. The object store consumes integer days, so a day-keyed mapping flows through without conversion.
- KTD2. Resolve retention at the record build. Governs R2, R3. Rationale. The build is the single point every producer passes through, so resolution is consistent and testable.
- KTD3. Fall back to the baseline for an unmapped classification. Governs R3. Rationale. A missing mapping entry must not drop retention, so the baseline is the floor.

### High-Level Design

A classification-to-retention mapping lands in `scripts/config.py`, parsed from an environment value into a dictionary of classification to days, with the existing baseline as the default. A resolver sets each evidence record's retention from its classification through the mapping, falling back to the baseline. The producers build records through the resolver, and the object store reads the integer-day retention as it does today. A unit test asserts a mapped classification resolves to its window, an unmapped one falls back, and the resolved value reaches the Object Lock put.

### Sequencing

U1 adds the mapping configuration. U2 resolves retention by classification at the record build. U2 depends on U1.

## Implementation Units

### U1. Add the classification-to-retention mapping

Goal. Add a configuration mapping from classification to retention days with the baseline default.
Requirements. R1.
Files. `scripts/config.py`, `.env.example`.
Approach. Add a setting parsed into a classification-to-days mapping, and keep the baseline retention setting as the fallback default. Document the format in `.env.example`.
Test scenarios. The mapping parses from the environment value. An empty mapping resolves to the baseline for every classification.
Verification. the settings test, `inv test`.

### U2. Resolve retention by classification at the record build

Goal. Set each evidence record's retention from its classification through the mapping.
Requirements. R2, R3, R4.
Files. `services/review/evidence.py` and the record-build path shared by the producers.
Approach. Add a resolver that maps a record's classification to retention days through the mapping, falling back to the baseline. Set the resolved value on the record when it is built. Confirm the object store reads the resolved integer-day retention into the Object Lock window.
Test scenarios. A mapped classification resolves to its retention. An unmapped classification falls back to the baseline. The resolved retention reaches the Object Lock put.
Verification. `tests/unit/test_object_store_evidence.py` additions, `inv test`.
