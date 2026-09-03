---
title: Pluggable Ticket-Tracker and Source-Control Backends - Plan
type: feat
date: 2026-09-02
origin: docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-brainstorm
execution: code
---

# Pluggable Ticket-Tracker and Source-Control Backends - Plan

## Goal Capsule

Objective. A deployment can run the platform against a non-Jira ticket tracker and a non-GitHub source-control target, and a deployment that configures neither behaves exactly as it does today.

Means. Extract a ticket-backend contract from the existing Jira adapter and add a rebar ticket-store backend, and add a local git-folder backend behind the existing `GitHost` contract, selected by config (KTD1, KTD2).

Product authority. This plan enriches one feature from the roadmap at [2026-09-02-1801-feat-remaining-work-roadmap-plan.md](docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md). That roadmap stays the requirements-only source; this is a separate implementation plan, not an in-place enrichment of the 20-feature roadmap.

Stop conditions. The default (Jira plus GitHub) path regresses no existing test. rebar's own ticket-store API and its S3 credential model are external and are consumed, not built here.

## Product Contract

Product Contract preservation. Sourced from the roadmap feature "Pluggable ticket-tracker and source-control backends"; requirements below restate that feature's subtasks as R-IDs with no scope change.

### Summary

Make the ticket tracker and the source-control target pluggable behind two orthogonal contracts. The ticket tracker defaults to Jira and gains a rebar ticket-store backend. The source-control target defaults to GitHub and gains a local git-folder backend that commits locally with no push and no pull request. Any combination composes from the two contracts. The only cross-point is mapping the source-control completion signal onto the ticket backend's terminal update.

### Problem Frame

The platform hardcodes two single-vendor assumptions. Ticket reads, status transitions, linkback, and approval recording go through the Jira adapter, and source control goes through GitHub via `RealGitHost`. A deployment without Jira, or without GitHub, cannot run. One target program has no Jira and needs the rebar ticket store, and another needs a local git folder with no remote.

### Requirements

- R1. A configuration value selects the ticket backend, defaulting to Jira. An unset value behaves as today.
- R2. A rebar ticket-store backend reads tickets, transitions status, records linkback and approval, and resolves the platform's logical lifecycle stages to the rebar store's own statuses.
- R3. A configuration value selects the source-control backend, defaulting to GitHub. An unset value behaves as today.
- R4. A local git-folder backend commits locally, performs no push, and opens no pull request. It leaves the promoted branch and a summary and patch artifact, and completes on an operator done command.
- R5. Ticket write-back (linkback, status transitions, approval recording) routes through the configured ticket backend under a write contract with per-project credentials, ticket-ownership validation against the run, least-privilege scopes, idempotent concurrency, and an audit record naming actor, backend, project, ticket, and run id.
- R6. A run completes from a merged or closed ticket status only after authenticated status provenance, a matching publish result, passed gates, and verified branch and artifact identifiers. When ticket status and source-control state disagree, a recorded precedence rule decides.
- R7. One flag-driven `inv` command polls the rebar tickets branch, reporting new tickets, updated fields, and status changes, under an authenticated reader identity restricted to approved roles, with non-allowlisted fields redacted and every invocation audited. It carries a lossless cursor contract, reading one branch-head snapshot against a persisted watermark, emitting ticket revision and branch commit identifiers, applying idempotency before side effects, and advancing the watermark only after intake or terminal signaling succeeds.
- R8. The rebar ticket store uses its own bucket or prefix with distinct IAM roles for read-only polling and for writes, denies cross-use by the evidence and audit writers, and includes actor and run provenance on tickets-branch ref writes.
- R9. The two backends are orthogonal contracts that compose without per-pair enumeration. The ticket backend owns read, write-back, linkback, approval, and status and terminal updates. The source-control backend owns the publish artifact and the completion signal.

### Scope Boundaries

- Seeding the rebar ticket store, including any Excel import, is rebar's own capability handled with the rebar author. This plan reads and writes an already-seeded store.
- rebar's S3 storage mechanism (`git-remote-s3`) is consumed, not modified.
- Jira and GitHub default behavior is unchanged.

### Outstanding Questions

- Deferred to planning. The exact rebar poll cursor source (ticket `updated_at`, a reduced-state hash, or the tickets-branch head plus per-ticket hashes). Resolve in U7. Non-blocking; the cursor contract in R7 stands regardless.
- Deferred to planning. Whether a local-folder completion resumes the existing `submit_pr_closed` signal path or a new provider-neutral workflow signal. Resolve in U6. Non-blocking.

### Sources

- Roadmap feature and its verification table: [2026-09-02-1801-feat-remaining-work-roadmap-plan.md](docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md).
- Ticket seam: `services/integration/jira/adapter.py` (`build_jira_adapter`, `build_jira_adapter_for_issue`, `JiraAdapter`), `services/integration/jira/credentials.py` (`build_jira_credential_resolver`, `JiraCredentialResolver`).
- Source-control seam: `adapters/interfaces/git_host.py` (`GitHost`), `adapters/real/git_host.py` (`RealGitHost`, `remote_url_for` seam), `services/integration/github/credentials.py` (`build_github_credential_resolver`).
- Workflow wiring: `services/orchestration/temporal_workflows/draft_pull_request.py`; activities in `services/orchestration/temporal_activities/pull_request.py` (`link_pr_to_issue`, `apply_task_status`, `record_pr_approval`, `create_draft_pr`, `find_open_pr`, `promote_pr`, `_build_jira_adapter`, `_build_mediator`).
- rebar Python API (installed `nava-rebar` 0.12.0): `rebar.show_ticket`, `rebar.list_tickets`, `rebar.transition`, `rebar.comment`, `rebar.create_ticket` (in-process over the git-backed store at `repo_root`).

## Planning Contract

### Key Technical Decisions

- KTD1. Extract a `TicketBackend` Protocol from the current `JiraAdapter` method set (`get_issue`, `get_status`, `transition`, `add_comment`, `list_comment_bodies`, `search_issues`, `create_issue`). The Jira adapter conforms unchanged. Governs R1, R2, R5. Rationale: every orchestration call site already uses these methods, so a Protocol plus a `build_ticket_backend` factory is the smallest seam that generalizes them.
- KTD2. Add the local git-folder backend behind the existing `GitHost` Protocol (`adapters/interfaces/git_host.py`), not a new interface. Governs R3, R4. Rationale: `GitHost` already abstracts source control and `RealGitHost` already accepts a `remote_url_for` seam, so a `LocalGitHost` sibling is the minimal addition.
- KTD3. The rebar ticket backend calls rebar's in-process Python API (`show_ticket`, `list_tickets`, `transition`, `comment`) against a local tickets worktree. Governs R2. Rationale: no subprocess or reconciler is required for read and write; the S3 sync is rebar's own remote.
- KTD4. Define a `PublishResult` carrying only source-control artifacts (branch, summary path, patch path, completion-signal id). Approval artifacts and status transitions stay with the ticket backend and fold into the workflow output after publish. Governs R6, R9. Rationale: `create_draft_pr` and `apply_task_status`/`record_pr_approval` are already separate activities, so coupling them in one result would break the backend boundary.
- KTD5. Backend selection is config-driven, defaulting to the current vendors. Add a `ticket_backend` key to `config/local/jira.yaml` scope (or a new `tickets.yaml`) and a `source_control_backend` key to `config/local/github.yaml` scope. Governs R1, R3. Rationale: mirrors the existing per-project and per-repo config shape.

### High-Level Design

Two Protocols, two factories, config selection, and one composition point.

- Ticket path. `build_ticket_backend(settings, *, project, resolver)` returns a `TicketBackend`. It returns the existing Jira adapter by default, or a `RebarTicketBackend` when configured. The orchestration helper `_build_jira_adapter` in `pull_request.py` becomes `_build_ticket_backend` and every call site (linkback, status, approval, intake, recheck, reconciler, notifications, worker validation) resolves through it.
- Source-control path. `build_git_host(settings, *, repo)` returns a `GitHost`, `RealGitHost` by default or `LocalGitHost` when configured. `_build_mediator` in `pull_request.py` resolves the host through it. `LocalGitHost` implements `ensure_branch` and `commit_and_push` locally, and satisfies `open_draft_pr`/`find_open_pr`/`mark_pr_ready`/`close_pr` by producing and inspecting a local branch plus a summary and patch artifact rather than a remote PR.
- Composition. The workflow drives the source-control backend to a `PublishResult`, then the ticket backend for linkback, approval, and terminal status. The terminal step honors R6.

### Assumptions

- rebar 0.12.0's documented Python API is stable enough to call directly; the plan pins to `show_ticket`/`list_tickets`/`transition`/`comment`.
- The rebar tickets worktree is available on the box that runs the ticket backend and the poll command.
- Existing Jira and GitHub credential resolvers are the credential source for the default backends and need no change.

### Sequencing

U1 and U4 are the two contract seams and come first, independently. U2 builds on U1. U3 is config and documentation isolation and can proceed independently of the others. U5 builds on U4. U6 and U8 build on U1, U4, and U5. U7 builds on U2. U6 depends on U5 for the publish result.

## Implementation Units

### U1. Ticket-backend contract and Jira default

Goal. Introduce a `TicketBackend` Protocol and a `build_ticket_backend` factory, with the Jira adapter as the default implementation, threaded through every orchestration call site.
Requirements. R1, R9.
Files. `services/integration/tickets/backend.py` (new Protocol), `services/integration/jira/adapter.py` (conform, add factory branch), `services/integration/jira/linkback.py` (`post_pr_linkback` dispatch by backend), `services/integration/tickets/factory.py` (new `build_ticket_backend`), `services/orchestration/temporal_activities/pull_request.py` (`_build_jira_adapter` to `_build_ticket_backend`), `services/orchestration/temporal_activities/intake.py`, `services/orchestration/temporal_activities/branch_sync.py`, `services/orchestration/notifications.py`, `services/orchestration/intake_reconciler.py`, `services/orchestration/worker.py`.
Approach. Define the Protocol from the seven `JiraAdapter` methods, plus a backend-owned linkback rendering method so Jira keeps ADF and wiki rendering through `client.source` while rebar receives plain ticket-store comment text. `post_pr_linkback` dispatches rendering by backend. Add `ticket_backend` config resolution defaulting to `jira` (KTD5). Route all call sites through `build_ticket_backend`. No behavior change when unset.
Test scenarios. Default config resolves the Jira backend. An unset key resolves Jira. The factory returns a `TicketBackend` conforming to the Protocol. Every migrated call site constructs through the factory. Existing Jira adapter tests pass unchanged.
Verification. `tests/unit/test_ticket_backend.py` (new), `tests/unit/test_jira_adapter.py` (unchanged, must pass), `inv test`.

### U2. Rebar ticket backend

Goal. Implement `RebarTicketBackend` conforming to `TicketBackend` over rebar's in-process API, with lifecycle-stage-to-status resolution.
Requirements. R2.
Files. `services/integration/tickets/rebar_backend.py` (new), `services/integration/tickets/factory.py` (register `rebar`), config schema for the rebar store (`repo_root`, status-flow map).
Approach. Map `get_issue`/`get_status`/`transition`/`add_comment`/`search_issues`/`create_issue` onto `rebar.show_ticket`/`transition`/`comment`/`list_tickets`/`create_ticket` (KTD3). Resolve logical stages (`work_started`, `ready_for_review`, terminal) to the store's configured statuses, reusing the status-flow-map shape from the Jira resolver. Add an explicit terminal status field to the status-flow config for both Jira and rebar, update the parser and validation, and change `apply_task_status` in `services/orchestration/temporal_activities/pull_request.py` to dispatch by known stage name and skip or reject an unknown stage deterministically, rather than mapping every non-`work_started` stage to `ready_for_review`.
Test scenarios. Read a ticket and its status through the backend. Transition a status maps the logical stage to the configured rebar status. A linkback posts a rebar comment. An unset lifecycle stage is skipped, never invented. Backend conforms to the Protocol.
Verification. `tests/unit/test_rebar_ticket_backend.py` (new, rebar API faked), `inv test`.

### U3. Rebar ticket-store credential isolation

Goal. Isolate the rebar ticket store's storage credentials from the evidence and audit writers.
Requirements. R8.
Files. `config/local/tickets.yaml.example` (new, bucket or prefix and role refs), `scripts/config.py` (settings), deployment docs under `docs/`.
Approach. Declare a dedicated bucket or prefix for the rebar store, distinct IAM role references for read-only polling and for writes, and deny cross-use by the evidence and audit store roles. Require actor and run provenance on tickets-branch ref writes. This unit is config and documentation; it provisions no cloud resources.
Test scenarios. Config parses the read-only and write role references. A rebar-store config that reuses the evidence bucket or prefix fails validation. A config where the read-only polling role equals the write role fails validation. A config where either role equals the evidence writer or audit writer role fails validation. Provenance fields are required on write config.
Verification. `tests/unit/test_tickets_store_config.py` (new), `inv test`.

### U4. Source-control backend selection and local git-folder backend

Goal. Add config-driven source-control selection defaulting to GitHub, and a `LocalGitHost` conforming to `GitHost`.
Requirements. R3, R4.
Files. `adapters/real/local_git_host.py` (new), `services/integration/github/factory.py` or extend the existing builder to `build_git_host` (new selection), `services/orchestration/temporal_activities/pull_request.py` (`_build_mediator` resolves through `build_git_host`), config `source_control_backend` key.
Approach. `LocalGitHost` implements `ensure_branch` and `commit_and_push` with local git and no push (KTD2). It satisfies `open_draft_pr`, `find_open_pr`, `mark_pr_ready`, and `close_pr` by producing and inspecting a local branch plus a summary and patch artifact rather than a remote PR. Selection defaults to `github`.
Test scenarios. Default config resolves `RealGitHost`. An unset key resolves GitHub. `LocalGitHost` commits without a push and creates no PR. `commit_and_push` writes the branch locally. Existing GitHub credential and mediator tests pass unchanged.
Verification. `tests/unit/test_local_git_host.py` (new), `tests/unit/test_github_credential_resolver.py` (unchanged), `inv test`.

### U5. Provider-neutral publish result and backend-neutral discovery

Goal. Define `PublishResult` and generalize PR discovery and reuse to a backend-neutral check.
Requirements. R4, R5, R9.
Files. `adapters/interfaces/git_host.py` (add `PublishResult`), `adapters/real/git_host.py` and `adapters/real/local_git_host.py` (fill it), `services/orchestration/temporal_workflows/pull_request.py` (replace `PullRequestOutput.number`/`url` and `OpenPrResult` with a provider-neutral publish output), `services/orchestration/temporal_workflows/draft_pull_request.py` (consume it, generalize `find_open_pr`/`EXISTING_PR_REUSED`).
Approach. `PublishResult` carries branch, summary path, patch path, and completion-signal id only (KTD4). Approval and status transitions stay on the ticket backend and are not in `PublishResult`, which fulfills R5's separation. Replace the PR-specific child-workflow output in `services/orchestration/temporal_workflows/pull_request.py` with the provider-neutral publish output before enabling `LocalGitHost`, and register the local branch and patch artifact on that output. Generalize the `EXISTING_PR_REUSED` discovery to a backend-neutral "existing branch that reached draft or merged status", which the local backend answers from the local branch state.
Test scenarios. `RealGitHost` fills `PublishResult` with a PR-derived completion-signal id. `LocalGitHost` fills it with a branch plus patch artifact. Discovery finds an existing local branch at draft or merged status. The result carries no approval or status field.
Verification. `tests/unit/test_local_git_host.py`, `tests/unit/test_draft_pull_request_workflow.py` (extend), `inv test`.

### U6. Operator done command and terminal completion hardening

Goal. Add the operator done command for the local backend and harden terminal completion for all backends.
Requirements. R4, R6.
Files. `services/orchestration/temporal_workflows/draft_pull_request.py` (terminal path, a done signal or reuse of `submit_pr_closed`), `services/orchestration/temporal_activities/pull_request.py` (`apply_task_status` terminal call at the terminal transition), an operator command under `scripts/`.
Approach. The operator done command authenticates an authorized operator, verifies the promoted branch and artifact identifiers, emits an audit event, and is idempotent. Terminal completion from a merged or closed ticket status proceeds only after authenticated status provenance, a matching `PublishResult`, passed gates, and verified identifiers. A recorded precedence rule decides when ticket status and source-control state disagree. Resolve the reuse-versus-new-signal question from Outstanding Questions here.
Test scenarios. The done command rejects an unauthorized operator. A forged or unmatched ticket status does not complete the run. A run completes when status, publish result, gates, and identifiers agree. Disagreement applies the recorded precedence. The done command is idempotent on repeat.
Verification. `tests/unit/test_draft_pull_request_workflow.py` (extend), `tests/end_to_end/` local-folder terminal scenario (new), `inv ci`.

### U7. Rebar change-detection poll command

Goal. Add the flag-driven `inv` poll over the rebar tickets branch with an access boundary and a lossless cursor.
Requirements. R7.
Files. `scripts/rebar_poll.py` (new), `tasks.py` (register the `inv` task), `services/orchestration/intake_reconciler.py` (consume for rebar intake), `services/orchestration/temporal_workflows/draft_pull_request.py` (human-approval consumes status output).
Approach. The command reports new tickets, updated fields, and status changes, with flags to select which. It runs under an authenticated ticket-backend reader identity restricted to approved roles, redacts non-allowlisted fields by default, and audits every invocation. It reads one branch-head snapshot against a persisted watermark, emits ticket revision and branch commit identifiers, applies idempotency before side effects, and advances the watermark only after intake or terminal signaling succeeds. Add a ticket-neutral intake candidate model to `services/orchestration/intake_reconciler.py` (task id, project, status, description, default repo, components, source backend, stored payload) that both Jira and rebar sources adapt into, replacing the Jira-shaped `normalize_issue` and `jira_source` assumption. Resolve the cursor source from Outstanding Questions here.
Test scenarios. A new ticket is reported once and not replayed after the watermark advances. A concurrent edit during a poll does not drop an update. An unauthorized caller is rejected. Non-allowlisted fields are redacted. Every invocation emits an audit record, including the unauthorized and no-op cases. Poll output and downstream status events carry the ticket revision and branch commit identifiers before any side effect runs. Intake starts a run from a new allowlisted ticket through the ticket-neutral candidate model. The human-approval step reads a status change.
Verification. `tests/unit/test_rebar_poll.py` (new), `tests/integration/` intake-from-rebar scenario (new), `inv ci`.

### U8. Write-back authorization and human-approval status wiring

Goal. Enforce the ticket write-back contract and let human approval read ticket status through the backend.
Requirements. R5, R6.
Files. `services/orchestration/temporal_activities/pull_request.py` (`link_pr_to_issue`, `apply_task_status`, `record_pr_approval` through the ticket backend with the write contract), `services/orchestration/temporal_workflows/draft_pull_request.py` (approval short-circuit reads ticket status via the backend or poll).
Approach. Every write-back call carries per-project credentials, validates ticket ownership against the run, uses least-privilege scopes, is idempotent under concurrency, and emits an audit record naming actor, backend, project, ticket, and run id. Add `issue_key` and project context to `RecordApprovalRequest` and its workflow call site so `record_pr_approval` builds the ticket backend from that identity, with a defined no-ticket skip. The human-approval step reads the current ticket status through the configured backend, using the poll command for rebar, and short-circuits to terminal only under the R6 guard.
Test scenarios. A write-back to a ticket not owned by the run is rejected. A write-back using another project's credentials, or scopes outside the configured write-back allowlist, is rejected. A duplicate write-back is idempotent. Each write-back emits an audit record that includes actor, backend, project, ticket, and run id. The approval short-circuit fires only when the R6 guard holds. Jira default write-back behavior is unchanged.
Verification. `tests/unit/test_jira_writes.py` (unchanged), `tests/unit/test_ticket_writeback_contract.py` (new), `tests/unit/test_draft_pull_request_workflow.py` (extend), `inv ci`.

## Verification Contract

- Lint. `ruff check` clean.
- Types. `basedpyright` clean on the changed packages.
- Unit and end-to-end. `inv ci` runs `ruff check`, `pytest -q tests/unit tests/end_to_end`, and the evaluation gate. All green.
- Full suite. `inv test` (`pytest`) green, including `tests/integration`.
- Regression floor. The default Jira-plus-GitHub path passes every pre-existing test in `tests/unit/test_jira_adapter.py`, `tests/unit/test_github_credential_resolver.py`, `tests/unit/test_draft_pull_request_workflow.py`, and `tests/integration/` with no change to their assertions.
- New coverage. Each new unit ships its named test file, and the local-folder terminal path and rebar intake path are proven under `tests/end_to_end/` and `tests/integration/`.

## Definition of Done

- Global. R1 through R9 are met. `inv ci` and `inv test` are green. The default backends behave exactly as before. Abandoned or experimental code from approaches that did not pan out is removed from the diff.
- U1. Every orchestration call site builds the ticket backend through `build_ticket_backend`; Jira is the default.
- U2. `RebarTicketBackend` conforms to the contract and maps lifecycle stages to rebar statuses.
- U3. The rebar-store credentials are isolated from the evidence and audit writers, validated by config tests that fail on read-write role equality and on evidence-or-audit role reuse.
- U4. `LocalGitHost` conforms to `GitHost`, commits with no push, and opens no PR; GitHub is the default.
- U5. `PublishResult` carries source-control artifacts only; discovery is backend-neutral.
- U6. The operator done command is authenticated, idempotent, and audited; terminal completion honors the R6 guard and precedence.
- U7. The poll command carries the access boundary and the lossless cursor, audits every invocation, emits ticket revision and branch commit identifiers before side effects, and drives rebar intake through the ticket-neutral candidate model and the approval status read.
- U8. Write-back enforces the authorization contract, including per-project credentials, least-privilege scopes, and an audit record naming actor, backend, project, ticket, and run id, and the human-approval short-circuit fires only under the R6 guard.
