---
title: Cross-Ticket Dependency Discovery on Jira - Plan
type: feat
date: 2026-09-11
origin: docs/plans/2026-09-03-1417-feat-cross-ticket-dependency-discovery-plan.md
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-brainstorm
execution: code
---

# Cross-Ticket Dependency Discovery on Jira - Plan

## Goal Capsule

Objective. Make cross-ticket dependency discovery a core capability that works on Jira as well as rebar, so a deployment that tracks work in a Jira project detects, records, waits on, and re-plans against undeclared dependencies exactly as a rebar deployment does.

Means. Split the dependency capability into two concerns. The per-run predicted touch set moves to a platform-owned store in the database, backend-neutral by construction. The dependency graph, the link and the declared-dependency and dependent reads, stays on the ticket backend and gains a Jira implementation over Jira issue links. Detection reads touch sets from the platform store and the graph from the ticket backend, so it runs on any backend that can link two tickets.

Product authority. This plan completes the cross-ticket dependency discovery work at [2026-09-03-1417-feat-cross-ticket-dependency-discovery-plan.md](docs/plans/2026-09-03-1417-feat-cross-ticket-dependency-discovery-plan.md), which shipped the rebar path and deferred Jira. It removes the rebar-only scope boundary and makes the feature backend-neutral.

Stop conditions. The rebar path regresses no existing test. A deployment with detection disabled behaves exactly as today. A Jira project with no configured dependency link type runs detection with the graph reads returning empty, so it records nothing and pauses on nothing rather than failing.

## Product Contract

### Summary

The cross-ticket dependency feature currently detects and resolves undeclared dependencies only on the rebar backend, because the capability that carries a run's file touch set and the capability that records a dependency are both bound to rebar's native store. This plan separates the two. A run's predicted touch set becomes platform run-state held in the database, readable by any run regardless of ticket backend. Recording a dependency, reading a ticket's blockers, and reading a ticket's dependents become ticket-graph operations that each backend implements with its own native mechanism, rebar links for rebar and issue links for Jira. With the split in place, detection, recording, pause, resume, re-base, re-plan, and the cycle and attempt guards all run unchanged on Jira.

### Problem Frame

Detection needs two inputs. It needs each in-flight run's predicted file touch set to compute overlap, and it needs the ticket dependency graph to record a discovered dependency, to find a blocking ticket's dependents when it completes, and to guard against cycles. The shipped design put both on the `DependencyBackend` capability and implemented them for rebar with `set_file_impact`, `get_file_impact`, `link`, `deps`, and inbound relationships. Jira has native issue links but no per-ticket file-impact store, so the whole capability is absent on Jira and detection is inactive there. The touch set is not ticket data. It is a property of the run, computed from the repository-intelligence context at the run's base. Holding it in the ticket store was an accident of rebar having a convenient place to put it. Moving it to the platform database removes the coupling and leaves only the graph operations, which Jira supports.

### Requirements

- R1. A run's predicted touch set is stored in a platform-owned table keyed by ticket id, written by the run and readable by any other run, independent of the ticket backend.
- R2. Detection reads its own and its peers' touch sets from the platform store rather than the ticket backend, and its behavior on rebar is unchanged.
- R3. The ticket backend exposes a dependency-graph capability of `link`, `declared_dependencies`, `dependents`, and a `dependency_graph_active` predicate that reports whether the graph is usable. rebar implements it over its native links and reports active unconditionally. Jira implements it over Jira issue links and reports active only when a dependency link type is configured.
- R4. Recording a dependency on Jira creates an issue link of a configured link type, so the dependent issue is blocked by the blocking issue and the relationship is visible in Jira and to the readiness query.
- R5. Reading a Jira ticket's blockers returns the issues linked as blocking it, and reading its dependents returns the issues it is linked as blocking, both parsed from the issue's links.
- R6. A run always publishes its own predicted touch set to the platform store, whatever the backend. Detection, recording, and pausing run only when the run's backend implements the dependency-graph capability and reports it active. A Jira project with no configured dependency link type publishes its touch set but records nothing and pauses on nothing.
- R7. Every behavior proven for rebar, detection, recording, pause, resume, re-base, re-plan, cycle guard, and attempt cap, holds on Jira through the same orchestration code with no branch on backend.

### Scope Boundaries

- The orchestration workflow, the detection algorithm, the pause and resume seam, the re-base and re-plan loop, and the guards are already implemented and are not rewritten. This plan changes only where the touch set is stored and adds the Jira graph implementation.
- The touch set stored in the database is the predicted set from the context package plus the ticket's declared impact, the same input the rebar path computes today.
- Jira link-type administration is out of scope. The deployment configures the name of an existing link type. The plan does not create link types in Jira.
- Mirroring the touch set into rebar's native file-impact store is out of scope. The database store is the single source of truth for detection.
- Cross-project resume with mixed link-type configuration is out of scope. The dependency link type resolves per project with a default fallback, so a deployment is expected to use one link type across projects that share a repository. A mixed configuration where a blocker's project has no link type leaves that blocker unable to report its dependents on completion, so a dependent recorded against it is not auto-resumed. Projects that share a repository must share a dependency link type.

### Resolved Decisions

- Resolved in planning. The touch set lives in a new `run_file_impact` table in the `agent_platform` schema, keyed by ticket id, with the paths and the writing execution id and an updated timestamp. A `FileImpactStore` reads and writes it. Detection uses the store for both backends, so the `record_file_impact` and `read_file_impact` methods leave the `DependencyBackend` capability.
- Resolved in planning. `DependencyBackend` narrows to the graph operations `link`, `declared_dependencies`, and `dependents`, plus a `dependency_graph_active` predicate. rebar keeps its implementation and reports active unconditionally. Jira gains one and reports active only when a dependency link type is configured. Detection resolves the graph through the ticket backend and the touch set through the store, and it detects, records, and pauses only when the backend implements the capability and reports it active.
- Resolved in planning. Activation is one authoritative signal, not the structural presence of the capability. The three dependency activities gate their side effects on `isinstance(backend, DependencyBackend) and backend.dependency_graph_active()`. Publishing the touch set is unconditional and precedes the gate, so a graph-inactive run still contributes its touch set for a graph-active peer to detect against, while it records and pauses on nothing itself. This replaces reliance on empty graph reads, which would not prevent a store-driven overlap from recording a link.
- Resolved in planning. The Jira link direction maps `link(dependent, blocking)` to an issue link where the blocking issue blocks the dependent issue, using the configured link type. `declared_dependencies` reads inward blocking links, and `dependents` reads outward blocking links, from the issue's links field already returned by `get_issue`. The Jira adapter carries the resolved per-project link type, so `link` uses it and `dependency_graph_active` reports `True` only when it is set.

### Sources

- Shipped dependency feature: `services/orchestration/dependency_detection.py`, `services/orchestration/temporal_activities/dependency.py`, `services/orchestration/temporal_workflows/draft_pull_request.py`.
- The capability protocol to split: `services/integration/tickets/backend.py` (`DependencyBackend`).
- rebar graph implementation to preserve: `services/integration/tickets/rebar_backend.py` (`link`, `declared_dependencies`, `dependents`).
- Jira client to extend: `services/integration/jira/adapter.py` (`JiraAdapter`, `_VendorJiraClient`).
- Jira config to extend: `config/local/jira.yaml` and `services/integration/jira/credentials.py`.
- Platform schema and migrations: `database/models/schema.py`, `database/migrations/versions`.

## Planning Contract

### Key Technical Decisions

- KTD1. Store the predicted touch set in a platform table rather than the ticket store. Governs R1, R2. Rationale: the touch set is run-state, not ticket data, and a platform table is available to every backend and every worker.
- KTD2. Narrow `DependencyBackend` to the graph operations and add a separate `FileImpactStore` for the touch set. Governs R2, R3. Rationale: the graph is genuinely per-backend, the touch set is not, so the split matches where the backend actually differs.
- KTD3. Implement the Jira graph over issue links, reading the links from the issue payload `get_issue` already returns and adding one client method to create a link. Governs R4, R5. Rationale: Jira issue links are the native dependency mechanism and most of the read path already exists.
- KTD4. Gate Jira detection on a configured dependency link-type name. Governs R6. Rationale: a link type is a Jira administration artifact the deployment must name, and its absence is a safe signal to run detection without a graph.
- KTD5. Keep the orchestration workflow and detection algorithm unchanged, routing touch-set reads to the store and graph reads to the backend behind the existing activity boundaries. Governs R7. Rationale: the shipped logic is tested, so the smallest change swaps the data sources the activities call.
- KTD6. Gate the dependency side effects on an explicit `dependency_graph_active` predicate rather than on the structural presence of the capability. Governs R3, R6. Rationale: after Jira implements the graph methods they are always present, so `isinstance` alone cannot express the config-driven inactive state; an explicit predicate is one authoritative signal that rebar reports true and Jira reports from its configured link type, and it keeps the activity gate meaningful.

### High-Level Design

A new `run_file_impact` table holds one row per ticket id with the predicted touch set and the writing execution id. The detect activity writes the current run's touch set to the store and reads each in-flight peer's touch set from the store, replacing the calls that went to the ticket backend. The `DependencyBackend` capability loses the file-impact methods and keeps `link`, `declared_dependencies`, and `dependents`. rebar keeps its implementation unchanged. The Jira adapter implements the three graph methods, creating an issue link of the configured type for `link`, and parsing the issue's links field for `declared_dependencies` and `dependents`. The detect, signal, cycle-check, and producer activities resolve the graph through the ticket backend and the touch set through the store. Detection is active when the ticket backend implements the graph capability, which rebar always does and Jira does when a dependency link type is configured.

### Assumptions

- The platform database is reachable from the activity worker, which it already is for execution state and audit.
- The Jira account the write role uses may create issue links of the configured type on the allowlisted projects.
- The repository-intelligence context resolves the impacted files and symbols at a commit, as the rebar path already relies on.

### Sequencing

U1 and U2 are independent. U3 builds on U2. U4 builds on U1 and U3. U5 is config and is independent. U6 spans the others.

## Implementation Units

### U1. Platform touch-set store

Goal. Move the predicted touch set from the ticket backend to a platform-owned table and store class.
Requirements. R1, R2.
Files. `database/models/schema.py` (a `run_file_impact` table), `database/migrations/versions` (a new migration), `services/orchestration/file_impact_store.py` (new, read and write by ticket id), `services/orchestration/temporal_activities/dependency.py` (use the store), `services/integration/tickets/backend.py` and `services/integration/tickets/rebar_backend.py` (drop `record_file_impact` and `read_file_impact` from the capability).
Approach. Add the table keyed by ticket id with the paths, the writing execution id, and an updated timestamp. Add a store that upserts and reads by ticket id. Change the detect activity to publish the current run's touch set to the store and read peers from it. Remove the file-impact methods from the capability and from rebar.
Test scenarios. The store round-trips a touch set. Detection over two runs finds overlap through the store. The rebar backend no longer needs a file-impact method for detection.
Verification. `tests/unit/test_file_impact_store.py` (new), `tests/unit/test_dependency_detection.py` (updated), `inv test`.

### U2. Jira issue-link client access

Goal. Expose the vendor Jira client's issue-link operations to the adapter.
Requirements. R4, R5.
Files. `services/integration/jira/adapter.py` (the `_VendorJiraClient` protocol gains `set_relationship` and `get_issue_links`).
Approach. The vendor Cloud and data-center clients already implement `set_relationship(from_key, to_key, link_type)`, which creates a link so the from issue blocks the to issue, and `get_issue_links(issue_key)`, which returns the issue's links in REST-nested shape. Declare both on the protocol as positional-only so the two transports' differing parameter names both satisfy it. No new REST code is required.
Test scenarios. The adapter's link path calls `set_relationship` with the blocking issue first. The read path parses `get_issue_links`.
Verification. `tests/unit/test_jira_dependency_graph.py` (new), `inv test`.

### U3. Jira dependency-graph capability

Goal. Implement `link`, `declared_dependencies`, `dependents`, and `dependency_graph_active` on the Jira adapter.
Requirements. R3, R4, R5, R6.
Files. `services/integration/jira/adapter.py` (`JiraAdapter` gains a `dependency_link_type` field and the graph methods), `services/integration/tickets/backend.py` (the narrowed `DependencyBackend` with the `dependency_graph_active` predicate), `services/integration/tickets/rebar_backend.py` (add `dependency_graph_active` returning `True`).
Approach. `link(dependent, blocking)` calls the vendor `set_relationship(blocking, dependent, link_type)` so the blocking issue blocks the dependent issue, and is idempotent by skipping when the dependent already declares the blocker. `declared_dependencies(issue)` returns the keys of issues linked as blocking the issue, and `dependents(issue)` returns the keys of issues the issue is linked as blocking, both parsed from `get_issue_links` filtered to the configured link type. `dependency_graph_active` returns whether the adapter carries a configured link type. The adapter always defines the methods; the predicate reports whether they are usable.
Test scenarios. A recorded link makes the dependent report the blocker and the blocker report the dependent. An issue with no dependency links reports empty. `dependency_graph_active` is true with a configured link type and false without.
Verification. `tests/unit/test_jira_dependency_graph.py` (new), `inv test`.

### U4. Route detection through the store and the graph

Goal. Make the detection, producer, and cycle activities resolve the touch set from the store and the graph from the ticket backend, for both backends, gated on one activation signal.
Requirements. R2, R6, R7.
Files. `services/orchestration/temporal_activities/dependency.py` (the detect, signal, and cycle activities).
Approach. Publish the run's own touch set to the store unconditionally, before any gate. Then treat a backend as graph-active only when `isinstance(backend, DependencyBackend) and backend.dependency_graph_active()`, and detect, record, pause, resume, and cycle-check only when active. Read peers' touch sets from the store and the graph through the ticket backend. Leave the workflow and the detection algorithm unchanged.
Test scenarios. Detection with a Jira backend records a dependency and pauses. The producer signals dependents read from Jira links. The cycle guard reads the Jira graph. A Jira project with no link type publishes its touch set but records and pauses on nothing.
Verification. `tests/unit/test_dependency_detection.py` (extended), `inv test`.

### U5. Jira dependency configuration

Goal. Let a deployment name the Jira dependency link type per project.
Requirements. R6.
Files. `config/local/jira.yaml.example`, `services/integration/jira/credentials.py` (parse a `dependency_link_type`), `README.md` (document it).
Approach. Add an optional `dependency_link_type` to the Jira config, resolved per project with the default as the fallback like the other fields. An empty value leaves the graph capability absent.
Test scenarios. A configured link type activates the capability. An empty value leaves it absent.
Verification. `tests/unit/test_config.py` (extended), `inv test`.

### U6. End-to-end and documentation

Goal. Prove the feature on Jira through the orchestration path and document it as backend-neutral.
Requirements. R7.
Files. `tests/integration` (a Jira dependency detection test with a fake client), `README.md` and `CHANGELOG.md`.
Approach. Exercise detect, record, pause, resume, re-base, re-plan on a Jira backend with a fake client and the platform store, asserting the same outcomes the rebar path asserts. Update the documentation to describe dependency discovery as a core feature available on both backends.
Test scenarios. A Jira run detects and records a dependency, pauses, and resumes to completion. The rebar suite is unchanged.
Verification. `inv ci`, `inv test`.

## Verification Contract

- Lint. `ruff check` clean.
- Types. `basedpyright` clean, no new baseline entries in the changed source files.
- Unit and end-to-end. `inv ci` green.
- Full suite. `inv test` green, including `tests/integration`.
- Regression floor. The rebar dependency suite passes with no change to its assertions, and detection disabled leaves the default path unchanged.
- New coverage. The store, the Jira link client method, the Jira graph capability, the Jira-routed detection, and the config each ship named tests.

## Definition of Done

- Global. R1 through R7 are met. `inv ci` and `inv test` are green. The rebar path is unchanged and the Jira path runs the same orchestration code. Abandoned or experimental code is removed from the diff.
- U1. The touch set is stored in the platform table and detection reads it there.
- U2. The Jira client can create an issue link.
- U3. The Jira adapter implements the dependency-graph capability over issue links.
- U4. Detection, the producer, and the cycle guard run on Jira through the store and the graph.
- U5. The Jira dependency link type is configurable per project.
- U6. The feature is proven on Jira end to end and documented as backend-neutral.
