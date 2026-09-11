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
- R2. During planning, before implementation, a run computes a predicted touch set and publishes it to the ticket store, then detects when its change depends on another in-flight run. The predicted touch set is the union of the repo-intelligence context package's impacted files at the run's base and the ticket's declared file impact. Detection compares that touch set against the published touch sets of other still-in-flight runs for file overlap, and flags an unresolved-symbol reference when a symbol undefined at the run's base has a candidate defining file inside a still-in-flight peer's published touch set. A backend without the file-impact capability runs with detection inactive.
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
- Recording a dependency is a `link` operation on the optional `DependencyBackend` capability, implemented for rebar. The wrapped Jira client exposes no issue-link API, so a Jira deployment does not implement the capability and does not participate in dependency discovery until a Jira issue-link and impact path is added. This plan implements the rebar path in full and defines the capability protocol the Jira path will conform to.

### Resolved Decisions

- Resolved in planning, U2. The dependency operations live on one optional `DependencyBackend` capability protocol: `link` records a dependency, `record_file_impact` and `read_file_impact` carry the per-run touch set, and `declared_dependencies` reads the dependency graph for the cycle guard. rebar implements the protocol over `rebar.link`, `rebar.set_file_impact`, `rebar.get_file_impact`, and `rebar.deps`. The core `TicketBackend` contract is unchanged, and the wrapped Jira client exposes no issue-link API, so Jira does not implement the capability and runs with dependency discovery inactive until a Jira issue-link and impact path is added. Detection and recording resolve the capability by an `isinstance` check and no-op when it is absent.
- Resolved in planning, U1. Detection draws both signals from planning-time data with no post-implementation input. The dependent run's predicted touch set is the union of the repo-intelligence context package's impacted files at the run's base and the ticket's declared file impact. Each run publishes that touch set to the ticket store so peers can read it. The file-overlap signal is the intersection of the run's touch set with a still-in-flight peer's published touch set. The unresolved-symbol signal is a symbol the run's context reports referenced but undefined at the run's base whose candidate defining file, taken from the context package's symbol locations, falls within a still-in-flight peer's published touch set. Both signals share one data source, the published per-run file impact, so neither depends on a peer's post-implementation output.
- Resolved in implementation, U1. The repo-intelligence context providers key on a symbol or file name, but intake sent the natural-language ticket title as the focus, so every symbol lookup missed and the context package returned no impacted files. This left the predicted touch set empty for every ticket and prevented detection from firing. Planning now forwards the ticket description alongside the title, and the context service extracts code-like identifiers and repository file paths from both, queries the index for each term, and unions the results into `symbols`, `references`, and `candidate_files`. A single-token focus with no description keeps the prior single-symbol behavior. This lives in `services/repository_intelligence/context_builder/focus.py` and `context_service.py`, with the description carried through `IntakeStart.description`, `DraftPullRequestInput.task_description`, and `ContextRequest.detail`.

### Known Limitations

- Detection is best-effort across a publication race. If two runs plan at nearly the same instant, the later publisher may not yet see the earlier run's touch set. The concurrency admission cap bounds the window, publication happens early at the end of planning, and the rebase-before-promote, gate, and repair backstops cover a dependency that detection misses on the first pass.
- The touch set is as precise as the context package's candidate files. For a symbol referenced across many files, the candidate set is every file that references the symbol rather than only the files the run edits, so overlap detection is broad and can flag two runs that touch a shared component surface without editing the same lines. The rebase-before-promote and gate backstops absorb an over-broad pause, and declared file impact narrows the set when a ticket provides it.

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
- KTD2. Record a discovered dependency through a `link` operation on the optional `DependencyBackend` capability, implemented for rebar with `rebar.link` and routed through the write-back contract. Governs R3. Rationale: converting the implicit dependency to a declared one makes it durable, visible to `rebar.ready`, and enforced on future passes and after restarts.
- KTD3. Promote the run's base commit from the frozen `params.base_sha` to a workflow instance variable so re-base and re-plan are reachable a second time. Governs R6. Rationale: planning currently runs once against a pinned base, so re-grounding requires a mutable base.
- KTD4. Draw both detection signals from planning-time data. The predicted touch set is the repo-intelligence context package's impacted files at the base unioned with the ticket's declared file impact, published back so peers can read it. Overlap is the intersection with a still-in-flight peer's published set, and the symbol signal maps a symbol undefined at the base to a candidate file through the context package and tests membership in a peer's set. Governs R2. Rationale: this gives both signals one durable data source available before implementation, so a run pauses without spending implementation compute.
- KTD5. Define the resume condition as the R6 completion guard's certified terminal state for the blocking ticket, not the blocking workflow's terminal alone. Governs R5. Rationale: the guard already certifies that changes are merged with matching identifiers, which is the precise meaning of present-in-base.
- KTD6. Put `link`, the cross-run file-impact publication and read, and the declared-dependency read on one optional `DependencyBackend` capability protocol implemented for rebar and absent on Jira, so dependency discovery is backend-neutral and inactive where the store cannot carry it. Governs R2, R3, R7. Rationale: keeps the core `TicketBackend` and the orchestration layer free of a direct rebar dependency and matches the per-backend capability pattern already used for recheck payloads and comment rendering.
- KTD7. Scope every detection pass, the first and each re-verification, to the runs still in flight from `list_active_executions`. Governs R5, R7. Rationale: a merged blocker leaves lasting file overlap in the base, so comparing against terminal runs would re-park forever. Restricting to in-flight runs makes resolution the natural consequence of the blocker reaching terminal completion, and it lets a still-unmet dependency on a different in-flight run re-park correctly.

### High-Level Design

Detection runs after context gathering and plan production. It builds the dependent run's predicted touch set from the repo-intelligence context package's impacted files at the base unioned with the ticket's declared file impact, and publishes that touch set to the ticket store through the file-impact capability. It then reads the published touch sets of the runs still in flight and flags a dependency on file overlap, or on a symbol undefined at the base whose candidate defining file falls inside a still-in-flight peer's touch set. On a hit, the run records the dependency on the ticket store through the write-back contract, then suspends on the dependency seam parked on the blocking ticket. A completion of the blocking ticket under the R6 guard emits the dependency-ready signal. On resume, the run re-captures the base-branch head into its base variable and re-runs the planning block. It then re-runs detection against the runs still in flight, so a blocker that has since completed no longer registers. A resolved dependency lets the run proceed to implementation. An unresolved dependency on another in-flight run re-parks, subject to an attempt cap and a cycle guard derived from the backend's declared-dependency read. Every step audits. A backend without the file-impact capability runs detection as a no-op.

### Assumptions

- The concurrency admission cap from the pluggable-backends work is present, so the number of in-flight runs to cross-reference is bounded.
- The repo-intelligence context package resolves the impacted files and the symbols defined at a given commit, so a symbol undefined at the base and a candidate defining file are both derivable from it.
- The ticket store exposes a dependency link, a readiness query, and a per-run file-impact read and write, satisfied by rebar. A backend without the file-impact capability, such as Jira today, runs detection as a no-op and is unaffected.

### Sequencing

U3 is an independent seam. U2 builds on the write-back contract and the ticket backend and adds the file-impact capability. U1 builds on U2 because detection reads and writes per-run file impact through that capability. U4 builds on U3. U5 builds on U4 and U1. U6 spans the others and is config and audit isolation. A practical order is U2, then U1 and U3 in parallel, then U4, then U5, then U6.

## Implementation Units

### U1. Planning-phase dependency detection

Goal. Add a detection check that computes and publishes a predicted touch set, then identifies a dependency on another in-flight run from file overlap and unresolved-symbol references.
Requirements. R1, R2.
Files. `services/orchestration/dependency_detection.py` (new), `services/orchestration/temporal_workflows/draft_pull_request.py` (call after plan production), `services/orchestration/temporal_activities/dependency.py` (new activity that computes the touch set from the context package, publishes it, and reads peers), `services/integration/tickets/backend.py` (the optional file-impact capability, defined in U2), config gate for the feature flag.
Approach. Derive the predicted touch set as the union of the context package's impacted files at the base and the ticket's declared file impact from `read_file_impact`. Publish it with `record_file_impact`. Enumerate the runs still in flight with `list_active_executions`, exclude self, and restrict to the same repository. Read each peer's published touch set with `read_file_impact`. Flag a file-overlap dependency on a non-empty intersection, and a symbol dependency when a symbol the context reports undefined at the base has a candidate defining file, taken from the context package's symbol locations, inside a peer's touch set. Return the blocking run and ticket, or none. Gate the whole check behind the feature flag, defaulting off, and return none when the backend does not implement the file-impact capability.
Test scenarios. Overlapping published touch sets yield a detection. A symbol undefined at the base whose candidate file is in a peer's touch set yields a detection. No overlap yields none. A peer that is no longer in flight yields none. A backend without the file-impact capability yields none. The flag off yields none and publishes nothing.
Verification. `tests/unit/test_dependency_detection.py` (new), `inv test`.

### U2. Record discovered dependency through the write-back contract

Goal. Record a discovered dependency on the ticket store as a declared dependency, and add the `DependencyBackend` capability the detection depends on.
Requirements. R3.
Files. `services/integration/tickets/backend.py` (add the optional `DependencyBackend` protocol with `link`, `record_file_impact`, `read_file_impact`, and `declared_dependencies`), `services/integration/tickets/rebar_backend.py` (implement the protocol over `rebar.link`, `rebar.set_file_impact`, `rebar.get_file_impact`, and `rebar.deps`), `services/orchestration/temporal_activities/pull_request.py` (a write-back activity for the link).
Approach. Define one optional capability protocol so the core `TicketBackend` and the orchestration layer stay free of a direct rebar dependency. Implement it for rebar. Route `link` through the write-back contract with ownership, a dedicated `link` scope, and audit, recording the dependent-to-blocking relationship so the ticket store's readiness query gates future passes. Leave Jira without the capability, since the wrapped client has no issue-link API.
Test scenarios. Recording a dependency links the two tickets. The write-back is idempotent on repeat. Recording without ownership is rejected. An audit record names both tickets and the run. The rebar backend round-trips a touch set through `record_file_impact` and `read_file_impact`. A backend without the capability is detected as absent by the `isinstance` check.
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
Files. `services/orchestration/dependency_detection.py` (re-verify entry), `services/orchestration/temporal_workflows/draft_pull_request.py` (proceed, re-park, or fail, with an attempt cap and a cycle guard from the backend's `declared_dependencies` read).
Approach. Re-run detection after the re-plan against the runs still in flight, so a blocker that has reached terminal completion no longer registers and the dependency reads as resolved. Proceed when resolved. Re-park when a dependency on another in-flight run remains, up to the attempt cap. Before re-parking, check the declared-dependency graph for a cycle back to the current ticket. Fail with a recorded reason when the cap is reached or a cycle is detected.
Test scenarios. A blocker that completed drops out of the in-flight set and the run proceeds. A still-unmet dependency on a different in-flight run re-parks. The attempt cap fails the run with a reason. A mutual dependency is detected as a cycle and fails both runs with a reason rather than ping-ponging.
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
- U1. A run publishes its predicted touch set, and detection identifies a dependency from file overlap and unresolved-symbol references against the runs still in flight. The check is gated by the feature flag and is inactive when the backend has no file-impact capability.
- U2. A discovered dependency is recorded through the write-back contract with ownership, idempotency, and audit.
- U3. A dependent run pauses on the dependency seam and resumes only on the blocking ticket's certified terminal under the R6 guard.
- U4. On resume the run re-captures its base and re-plans against it before implementation.
- U5. After re-planning the run verifies resolution and bounds the loop with an attempt cap and a cycle guard.
- U6. Every dependency step is audited and the feature is config-gated, defaulting off.
