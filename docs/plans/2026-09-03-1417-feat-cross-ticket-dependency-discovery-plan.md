---
title: Cross-Ticket Dependency Discovery and Re-Plan - Plan
type: feat
date: 2026-09-03
origin: docs/plans/2026-09-02-2022-feat-pluggable-ticket-source-control-backends-plan.md
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-brainstorm
execution: code
---

# Cross-Ticket Dependency Discovery and Re-Plan - Plan

## Goal Capsule

Objective. When two runs are admitted from independent tickets and one turns out to depend on the other, the dependent run detects the dependency during planning, records it, waits for the other run's changes to land in its base, re-plans against that updated base, and proceeds only after verifying the dependency is resolved.

Means. Add a planning-phase detection check over file-impact overlap and unresolved-symbol references, record the discovered dependency into the ticket store through the write-back contract, pause the dependent run on the existing dependency suspend seam, and add a re-base and re-plan loop that re-grounds the run before it continues.

Product authority. This plan enriches the pluggable ticket and source-control backends work at [2026-09-02-2022-feat-pluggable-ticket-source-control-backends-plan.md](docs/plans/2026-09-02-2022-feat-pluggable-ticket-source-control-backends-plan.md). It is scheduled into v0.1.0 as a separate unit of work and lands as its own commit.

Stop conditions. The default path, with detection disabled, regresses no existing test. Semantic dependencies that produce no file overlap and no planned-symbol reference remain out of structural detection and rely on the existing rebase, gate, and repair backstops.

## Product Contract

### Summary

A run that is admitted alongside another run can discover, during its own planning phase, that its change depends on the other run's change even when no dependency was declared on the tickets. The dependent run records the discovered dependency on the ticket store, pauses without holding compute, and resumes only once the blocking ticket's changes are present in the dependent run's base branch. On resume it re-captures its base and re-plans, then verifies the dependency is resolved before proceeding. This closes the gap where a held run would otherwise resume against a stale base and produce broken or conflicting code.

### Problem Frame

Runs pin their base commit once at intake and plan once against it. Two tickets with no declared dependency can be admitted together. If ticket 2's change in fact depends on ticket 1's change, ticket 2 plans against a base without ticket 1, and nothing re-grounds it. Serializing the two runs alone does not help, because a run resumed on the other run's terminal state still holds a stale base and a plan computed without the dependency present. The dependent run needs to re-base and re-plan against a tree that contains the dependency, then confirm the dependency is resolved, before it continues.

### Requirements

- R1. A configuration value enables cross-ticket dependency detection, defaulting to off. With detection off, a run behaves exactly as it does today.
- R2. During planning, a run detects when its planned change depends on another in-flight run. Detection uses file-impact overlap between the run's planned file set and other in-flight runs, and unresolved-symbol references where the plan needs a symbol that is absent at the run's base but is introduced by another in-flight run.
- R3. A discovered dependency is recorded on the ticket store as a declared dependency through the write-back contract, naming the dependent ticket, the blocking ticket, and the discovering run, under an audit record.
- R4. A run whose dependency is unmet pauses at a dependency checkpoint without holding compute, parked on the blocking ticket identifier.
- R5. A paused run resumes only after the blocking ticket reaches verified terminal completion under the R6 completion guard, and only when the blocking ticket's changes are present in the paused run's base branch.
- R6. On resume, the run re-captures its base from the current base-branch head and re-plans against it before proceeding to implementation.
- R7. After re-planning, the run re-verifies the dependency is resolved. It proceeds when resolved, re-parks when a dependency remains unmet, or fails with a recorded reason when the dependency cannot be resolved. A bounded re-plan attempt cap and a dependency-cycle guard prevent unbounded loops and mutual-dependency ping-pong.
- R8. Detection, recording, pause, resume, re-base, and re-plan each emit an audit record naming the run, the dependent and blocking tickets, and the decision.

### Scope Boundaries

- Declared dependencies already gate intake through the ticket store's readiness query. This plan adds discovery of undeclared dependencies and the re-grounding loop. It does not change the existing readiness gate.
- Semantic dependencies with no file overlap and no planned-symbol reference are not structurally detectable. They remain covered by rebase-before-promote, the CI and acceptance gates, and the repair loop.
- The concurrency admission cap is delivered with the pluggable-backends work and is a precondition here, not part of this plan.
- Recording a dependency on a Jira backend uses Jira issue links under the same write-back contract. This plan implements the rebar link path and defines the contract method that the Jira path conforms to.

### Outstanding Questions

- Deferred to planning. Whether the `TicketBackend` contract gains a `link` method or the dependency record routes through a separate capability. Resolve in U2. Non-blocking.
- Deferred to planning. The exact unresolved-symbol signal source, the repo-intelligence context package against the base commit versus a lighter reference check. Resolve in U1. Non-blocking.

### Sources

- Pluggable backends plan and its ticket-neutral candidate and write-back contract: [2026-09-02-2022-feat-pluggable-ticket-source-control-backends-plan.md](docs/plans/2026-09-02-2022-feat-pluggable-ticket-source-control-backends-plan.md).
- Suspend and resume seam, already present with an unused dependency reason: `services/orchestration/suspension.py` (`SuspendReason.AWAITING_DEPENDENCY`, `ResumeOn.DEPENDENCY_READY`).
- Planning and base pin: `services/orchestration/temporal_workflows/draft_pull_request.py` (`run` planning block, `params.base_sha`), `services/orchestration/intake_runtime.py` (`remote_head`, `base_sha`).
- Terminal completion guard defining present-in-base: `services/orchestration/terminal_guard.py` (`evaluate_completion`).
- Write-back contract: `services/integration/tickets/write_contract.py`.
- rebar dependency and file-impact API (installed `nava-rebar` 0.12.0): `rebar.link`, `rebar.unlink`, `rebar.deps`, `rebar.find_inbound_relationships`, `rebar.get_file_impact`, `rebar.set_file_impact`, `rebar.ready`.
- Repo-intelligence context: `services/orchestration/temporal_activities/planning.py` (`gather_repo_context`).

## Planning Contract

### Key Technical Decisions

- KTD1. Reuse the existing `SuspendReason.AWAITING_DEPENDENCY` and `ResumeOn.DEPENDENCY_READY` seam rather than adding a new suspend reason. Governs R4, R5. Rationale: the scaffolding exists and is unused, so wiring a producer and a consumer is the smallest change.
- KTD2. Record a discovered dependency by adding a `link` capability to the ticket backend, implemented for rebar with `rebar.link` and routed through the write-back contract. Governs R3. Rationale: converting the implicit dependency to a declared one makes it durable, visible to `rebar.ready`, and enforced on future passes and after restarts.
- KTD3. Promote the run's base commit from the frozen `params.base_sha` to a workflow instance variable so re-base and re-plan are reachable a second time. Governs R6. Rationale: planning currently runs once against a pinned base, so re-grounding requires a mutable base.
- KTD4. Detect overlap from the plan's declared file set combined with `rebar.get_file_impact`, and detect unresolved symbols from the repo-intelligence context resolved at the run's base. Governs R2. Rationale: both signals are already produced near the planning phase and need only cross-referencing against in-flight runs.
- KTD5. Define the resume condition as the R6 completion guard's certified terminal state for the blocking ticket, not the blocking workflow's terminal alone. Governs R5. Rationale: the guard already certifies that changes are merged with matching identifiers, which is the precise meaning of present-in-base.

### High-Level Design

Detection runs after context gathering and plan production. It builds the dependent run's file set from the plan plus the ticket's declared file impact, then queries in-flight runs for overlapping file impact and for symbols the plan references that are absent at the base. On a hit, the run records the dependency on the ticket store through the write-back contract, then suspends on the dependency seam parked on the blocking ticket. A completion of the blocking ticket under the R6 guard emits the dependency-ready signal. On resume, the run re-captures the base-branch head into its base variable and re-runs the planning block. It then re-runs detection. A resolved dependency lets the run proceed to implementation. An unresolved dependency re-parks, subject to an attempt cap and a cycle guard derived from `rebar.deps`. Every step audits.

### Assumptions

- The concurrency admission cap from the pluggable-backends work is present, so the number of in-flight runs to cross-reference is bounded.
- The repo-intelligence context can resolve symbol presence at a given commit.
- The ticket store exposes a dependency link and a readiness query, satisfied by rebar and by Jira issue links.

### Sequencing

U1 and U3 are independent seams. U2 builds on the write-back contract and the ticket backend. U4 builds on U3. U5 builds on U4 and U1. U6 spans the others and is config and audit isolation.

## Implementation Units

### U1. Planning-phase dependency detection

Goal. Add a detection check that identifies a dependency on another in-flight run from file-impact overlap and unresolved-symbol references.
Requirements. R1, R2.
Files. `services/orchestration/dependency_detection.py` (new), `services/orchestration/temporal_workflows/draft_pull_request.py` (call after plan production), `services/orchestration/temporal_activities/planning.py` (expose the plan file set and symbol references), config gate for the feature flag.
Approach. Build the dependent run's file set from the plan and the ticket's declared file impact. Query in-flight runs for overlapping file impact and for symbols the plan references that are absent at the base. Return a detection result naming the blocking run and ticket, or none. Gate the whole check behind the feature flag, defaulting off.
Test scenarios. Overlapping file impact yields a detection. An unresolved symbol introduced by another in-flight run yields a detection. No overlap yields none. The flag off yields none.
Verification. `tests/unit/test_dependency_detection.py` (new), `inv test`.

### U2. Record discovered dependency through the write-back contract

Goal. Record a discovered dependency on the ticket store as a declared dependency.
Requirements. R3.
Files. `services/integration/tickets/backend.py` (add a `link` method to the contract), `services/integration/tickets/rebar_backend.py` (implement with `rebar.link`), `services/integration/jira/adapter.py` (implement with Jira issue links), `services/orchestration/temporal_activities/pull_request.py` (a write-back activity for the link).
Approach. Add a backend `link` capability and route it through the write-back contract with ownership, scope, and audit. Record the dependent-to-blocking relationship so the ticket store's readiness query gates future passes. Resolve the contract-method question from Outstanding Questions here.
Test scenarios. Recording a dependency links the two tickets. The write-back is idempotent on repeat. Recording without ownership is rejected. An audit record names both tickets and the run.
Verification. `tests/unit/test_dependency_writeback.py` (new), `inv test`.

### U3. Dependency pause and resume wiring

Goal. Pause a run on the dependency seam and resume it on the blocking ticket's certified completion.
Requirements. R4, R5.
Files. `services/orchestration/temporal_workflows/draft_pull_request.py` (suspend on `AWAITING_DEPENDENCY`, a dependency-ready signal), `services/orchestration/terminal_guard.py` (surface the certified terminal for the blocking ticket).
Approach. Suspend the dependent run parked on the blocking ticket. Emit the dependency-ready signal when the blocking ticket reaches verified terminal completion under the R6 guard. Resume only on that signal.
Test scenarios. A run with an unmet dependency parks. A blocking terminal that fails the R6 guard does not resume the parked run. A certified blocking terminal resumes it.
Verification. `tests/unit/test_draft_pull_request_workflow.py` (extend), `inv test`.

### U4. Re-base and re-plan loop

Goal. On resume, re-capture the base and re-plan before proceeding.
Requirements. R6.
Files. `services/orchestration/temporal_workflows/draft_pull_request.py` (base as an instance variable, re-enterable planning block, re-capture base head on resume).
Approach. Promote `base_sha` to a workflow variable. On resume, re-capture the base-branch head and re-run context gathering and planning against it. Carry the updated base into implementation.
Test scenarios. On resume the base advances to the base-branch head. Planning runs a second time against the updated base. Implementation uses the updated base.
Verification. `tests/unit/test_draft_pull_request_workflow.py` (extend), `inv test`.

### U5. Re-verify with cycle and attempt guards

Goal. After re-planning, verify the dependency is resolved and bound the loop.
Requirements. R7.
Files. `services/orchestration/dependency_detection.py` (re-verify entry), `services/orchestration/temporal_workflows/draft_pull_request.py` (proceed, re-park, or fail, with an attempt cap and a cycle guard from `rebar.deps`).
Approach. Re-run detection after the re-plan. Proceed when resolved. Re-park when a dependency remains, up to the attempt cap. Fail with a recorded reason when the cap is reached or a dependency cycle is detected.
Test scenarios. A resolved dependency proceeds. A still-unmet dependency re-parks. The attempt cap fails the run with a reason. A mutual dependency is detected as a cycle and fails both runs with a reason rather than ping-ponging.
Verification. `tests/unit/test_dependency_detection.py` (extend), `tests/unit/test_draft_pull_request_workflow.py` (extend), `inv ci`.

### U6. Audit and configuration isolation

Goal. Audit every dependency step and gate the feature behind config.
Requirements. R1, R8.
Files. config schema for the feature flag and the attempt cap, `services/orchestration/dependency_detection.py` and the workflow (audit records).
Approach. Add the feature flag, defaulting off, and the attempt cap to config. Emit an audit record on detection, record, pause, resume, re-base, re-plan, and terminal decision, each naming the run and the dependent and blocking tickets.
Test scenarios. The flag off skips detection entirely. Each step emits an audit record. The attempt cap is read from config.
Verification. `tests/unit/test_dependency_detection.py` (extend), `inv ci`.

## Verification Contract

- Lint. `ruff check` clean.
- Types. `basedpyright` clean on the changed packages.
- Unit and end-to-end. `inv ci` runs `ruff check`, `pytest -q tests/unit tests/end_to_end`, and the evaluation gate. All green.
- Full suite. `inv test` green, including `tests/integration`.
- Regression floor. With detection disabled, the draft-PR workflow tests and the intake tests pass with no change to their assertions.
- New coverage. Detection, recording, pause and resume, re-base and re-plan, and the cycle and attempt guards each ship named tests.

## Definition of Done

- Global. R1 through R8 are met. `inv ci` and `inv test` are green. With detection disabled the default path is unchanged. Abandoned or experimental code is removed from the diff.
- U1. Detection identifies a dependency from file-impact overlap and unresolved-symbol references and is gated by the feature flag.
- U2. A discovered dependency is recorded through the write-back contract with ownership, idempotency, and audit.
- U3. A dependent run pauses on the dependency seam and resumes only on the blocking ticket's certified terminal under the R6 guard.
- U4. On resume the run re-captures its base and re-plans against it before implementation.
- U5. After re-planning the run verifies resolution and bounds the loop with an attempt cap and a cycle guard.
- U6. Every dependency step is audited and the feature is config-gated, defaulting off.
