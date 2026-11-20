---
title: Remaining Connectivity and Sandbox Confirmations - Plan
type: feat
date: 2026-09-14
origin: docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-brainstorm
execution: code
---

# Remaining Connectivity and Sandbox Confirmations - Plan

## Goal Capsule

Objective. The open connectivity and sandbox questions are confirmed and reflected in config and adapters, so the deployed-workload credential method, the OpenHands sandbox design, and the Jira, GHES, and security-tool integration formats are settled rather than assumed.

Means. Confirm the deployed-workload credential method, confirm the OpenHands sandbox design against the running sandbox, and confirm the Jira, GHES, and security-tool integration formats, reflecting each confirmation in `scripts/config.py` and the relevant adapters.

Product authority. This plan enriches one feature from the roadmap at [2026-09-02-1801-feat-remaining-work-roadmap-plan.md](2026-09-02-1801-feat-remaining-work-roadmap-plan.md), the "Remaining connectivity and sandbox confirmations" entry. It does not widen that feature's scope.

Stop conditions. Each open item is confirmed and reflected in config and adapters, or recorded as still open with the reason. The low-confidence effort range reflects that some confirmations may surface follow-on work.

## Product Contract

Product Contract preservation. Sourced from the roadmap feature "Remaining connectivity and sandbox confirmations". The requirements below restate that feature's subtasks as R-IDs with no scope change.

### Summary

The jump-host and DNS path and the local profile credential selection are confirmed, and since the roadmap captured that state the jump-host path has been exercised live and the OpenHands sandbox has run end to end. The deployed-workload credential method, the OpenHands sandbox design confirmation, and the Jira, GHES, and security-tool integration-format confirmations remain. This plan confirms each and reflects the outcome in config and adapters, or records an item as still open with its reason.

### Problem Frame

Several deployment questions were left open because they could not be settled without a running system. The platform now runs the jump-host path live, runs the OpenHands sandbox, and drives Jira and GitHub adapters, so the questions are answerable against a running system rather than in the abstract. The deployed-workload credential method decides how an in-VPC workload obtains AWS credentials without a laptop profile. The OpenHands sandbox design confirmation checks the sandbox posture against what the runtime actually does. The integration-format confirmations check the Jira, GHES, and security-tool formats against the adapters.

### Requirements

- R1. The deployed-workload credential method is confirmed and reflected in config, or recorded as open with the reason.
- R2. The OpenHands sandbox design is confirmed against the running sandbox and reflected in config where it differs.
- R3. The Jira, GHES, and security-tool integration formats are confirmed and reflected in the adapters and config.
- R4. Each confirmation is recorded so a reader knows what was settled and what remains open.

### Scope Boundaries

- This plan confirms and reflects settled facts. A confirmation that surfaces substantial new build work records that work rather than absorbing it.
- The jump-host and DNS path and the local profile selection are already confirmed and are not revisited except to reference them.
- This plan does not change the connectivity provider contract, which is delivered.

### Resolved Decisions

- Resolved in planning. Confirmations are grounded against the running system, since the jump-host, the sandbox, and the adapters now run, so a confirmation is an observation rather than a guess.
- Resolved in planning. An item that cannot be settled is recorded as open with its reason rather than forced to a premature answer, consistent with the low-confidence effort.

### Sources

- Roadmap feature and verification table: [2026-09-02-1801-feat-remaining-work-roadmap-plan.md](2026-09-02-1801-feat-remaining-work-roadmap-plan.md).
- Connectivity and credential settings: `scripts/config.py` (`aws_profile`, the connectivity provider and jump-host settings).
- Sandbox settings and runtime: `scripts/config.py` (the OpenHands and sandbox settings), `services/orchestration/temporal_activities/implementation.py`, `services/orchestration/openhands_code_step.py`.
- Integration adapters: `services/integration/jira`, `services/integration/github`, the security-tool integrations.

## Planning Contract

### Key Technical Decisions

- KTD1. Ground each confirmation against the running system. Governs R1, R2, R3. Rationale. The jump-host, sandbox, and adapters run now, so a confirmation observes behavior rather than reasoning in the abstract.
- KTD2. Reflect a confirmed fact in config or the adapter, not only in prose. Governs R1, R2, R3. Rationale. A confirmation that changes a setting or a default belongs in the code, so the settled fact is enforced.
- KTD3. Record an unsettled item as open with its reason. Governs R4. Rationale. The effort is low-confidence, so an honest record of what remains open is part of the deliverable.

### High-Level Design

Each of the three open items is confirmed in turn. The deployed-workload credential method is confirmed against the AWS credential chain an in-VPC workload uses and reflected in the config default. The OpenHands sandbox design is confirmed against the running sandbox and its config, with any divergence reflected in the settings. The Jira, GHES, and security-tool formats are confirmed against the adapters and reflected in config where a format assumption is encoded. A short confirmations note records what was settled and what remains open.

### Assumptions

- The running system is representative of the deployed posture for the behaviors being confirmed.

### Sequencing

U1 confirms the deployed-workload credential method. U2 confirms the OpenHands sandbox design. U3 confirms the integration formats. U4 records the confirmations. The confirmation units are independent, and U4 depends on them.

## Implementation Units

### U1. Confirm the deployed-workload credential method

Goal. Confirm how an in-VPC deployed workload obtains AWS credentials and reflect it in config.
Requirements. R1.
Files. `scripts/config.py`, the credential-resolution path.
Approach. Confirm the credential chain an in-VPC workload uses, the task role rather than a laptop profile, against the direct connectivity posture. Reflect the confirmed default in config, or record the item as open with the reason if it cannot be settled.
Test scenarios. The confirmed credential method is reflected in a config default or recorded as open. The local profile path is unchanged.
Verification. the settings test, `inv test`.

### U2. Confirm the OpenHands sandbox design

Goal. Confirm the OpenHands sandbox design against the running sandbox and reflect any divergence in config.
Requirements. R2.
Files. `scripts/config.py` (the OpenHands and sandbox settings), `services/orchestration/temporal_activities/implementation.py`, `services/orchestration/openhands_code_step.py`.
Approach. Confirm the sandbox posture, the runtime class, the namespace, the resource and tool-call bounds, and the credential and network scope, against what the running sandbox does. Reflect any confirmed divergence in the settings.
Test scenarios. The confirmed sandbox posture matches the settings, or a divergence is reflected in config. The running sandbox behavior matches the recorded design.
Verification. the sandbox settings test, `inv test`, an observation of a sandbox run.

### U3. Confirm the integration formats

Goal. Confirm the Jira, GHES, and security-tool integration formats and reflect them in the adapters and config.
Requirements. R3.
Files. `services/integration/jira`, `services/integration/github`, the security-tool integrations, `scripts/config.py`.
Approach. Confirm each integration format against the adapter that consumes it, the Jira field and auth formats, the GHES API and token format, and the security-tool output formats. Reflect any confirmed format assumption in config or the adapter.
Test scenarios. Each confirmed format matches the adapter, or a divergence is reflected. A format assumption encoded in config matches the adapter.
Verification. the adapter tests, `inv test`.

### U4. Record the confirmations

Goal. Record what was settled and what remains open.
Requirements. R4.
Files. `docs/connectivity-and-sandbox-confirmations.md` (new) or a section in the local-to-AWS mapping document.
Approach. Record each confirmed item and its outcome, and each item that remains open with its reason and the follow-on work it needs.
Test scenarios. Every open item from the roadmap is recorded as confirmed or open. Each open item names its reason and follow-on work.
Verification. Document review against the confirmations.
