---
title: Remaining Work Roadmap - Plan
type: feat
date: 2026-09-02
topic: remaining-work-roadmap
artifact_contract: ce-unified-plan/v1
artifact_readiness: requirements-only
product_contract_source: ce-brainstorm
execution: code
---

# Remaining Work Roadmap - Plan

## Goal Capsule

Objective. Close the verified remaining work on the agentic SDLC platform, presented as a list of named features with discrete subtasks and an effort estimate.

Product authority. Status is established by code verification against the committed repository, not by the annotations in `.agent-context/agentic_plan_v2.md`, which are stale for the later work. This is a flat feature list with no priority order, no phase numbering, no bucketing, and no ticket prefix. Grouping features into versions and setting a release tempo is a separate document.

Open blockers. Two items are blocked on an external rebar 0.13 release and are held rather than listed as features. Two rebar-adoption decisions remain open. Each feature carries an effort in hours of uninterrupted work.

## Product Contract

### Summary

The platform's core pipeline is delivered and code-verified. From a Jira ticket it plans, codes, runs checks, reviews through the rebar multi-pass kernel, applies OPA promotion, and opens a draft pull request with Jira linkback, under human-in-the-loop approval and audit. This roadmap lists everything that remains, grounded in verification, and adds two new configurable backends. The first tracks work in rebar's ticket store, a git branch synced to a private S3 bucket, instead of Jira. Seeding that store, including any Excel import, is rebar's own capability handled with the rebar author, outside this feature. The second targets a local git-initialized folder instead of GitHub, with no push and no pull request.

### Key Decisions

- Both new backends are configurable and default to current behavior. The ticket tracker defaults to Jira and the source-control backend defaults to GitHub. A deployment that sets nothing behaves exactly as today.
- The pluggable ticket tracker uses rebar's ticket store as its non-Jira backend. rebar keeps the ticket event log on a dedicated git orphan branch in a separate `.tickets-tracker` worktree, never in the platform code repo or on a GitHub working branch, and the S3 option syncs that branch to a private bucket as per-ref bundles through `git-remote-s3`. This satisfies the `agentic_plan_v2.md` section 10 sensitive-data concern because ticket data never lands on a shared git host and is governed by the bucket confidentiality controls, though the store remains git-structured. Adopting it makes rebar's store the source of truth for a no-Jira deployment, with Jira remaining the default.
- Seeding the rebar ticket store, including any Excel import, is rebar's own capability handled with the rebar author, outside this feature. The feature reads and writes the rebar store, which is the store of record for a no-Jira deployment.
- In local-folder source-control mode the checks, review, and OPA promotion stages run unchanged, because they already run before the pull request opens. The publish and post-publish stages are not a single terminal swap. Opening the pull request, the ticket linkback, the human-approval recording, and pull-request discovery and reuse all assume a pull request today, so the local backend defines a provider-neutral equivalent for each. The platform leaves the promoted branch and a summary and patch artifact, a human merges, and an operator done command replaces the pull-request-closed webhook as the terminal signal.
- The JavaScript verification corpus is genuine remaining work, not redundant. It is a dedicated JavaScript-only demo repository with no `tsconfig.json` that exercises the SCIP indexer's inferred-config path, which the shared TypeScript corpus does not cover.
- Authored-identity signing depends on the same S3 and MinIO object store that the pluggable ticket-tracker and source-control backends feature introduces.

### Features

Each feature lists its code-verified current state, its target outcome, discrete subtasks, and an effort in hours of uninterrupted work. Discovery-dependent features give an hours range with a confidence of low, medium, or high and keep the discovery or confirmation subtask separate from the build subtasks. Well-understood features give a single-hour estimate.

#### Native Bedrock review path

Current state. Held. The review kernel still runs on the LiteLLM OpenAI-compatible proxy path.
Target. The rebar review kernel runs on the native Bedrock path with the in-process Claude-to-Nova fallback chain and unchanged accounting.

Subtasks.
- Swap the review-kernel model provider from the LiteLLM proxy to native Bedrock with the Claude-to-Nova fallback chain.
- Mount AWS credentials into the review worker.
- Verify per-leg failover and that token and cost accounting match the proxied path.
- Scope the review worker to short-lived, least-privilege STS credentials for Bedrock review calls, with rotation, environment separation, and log redaction.

Effort. 6h

Reference.

| Inspected locations | Verification result | Revision |
|---|---|---|
| `services/model_gateway/llm_config.py`, `services/orchestration/temporal_activities/bedrock.py` | Held. Kernel on the LiteLLM OpenAI-compatible path (runner pydantic_ai, provider openai, via `litellm_base_url`), not native Bedrock. | 641d605 |

#### Signed review verdicts

Current state. Not started. `services/review/attestation.py` still uses the platform HMAC attestor.
Target. Plan and code review verdicts carry a rebar attest SSHSIG signature verified against a public trust root, and the gate fails closed on an absent or invalid signature.

Subtasks.
- Replace the platform HMAC attestor with the rebar attest SSHSIG substrate for plan and code verdicts.
- Supply and load the public trust root.
- Add signature verification to the gate with fail-closed on an absent or invalid signature.
- Ship a versioned trust-root bundle with key identifiers, revocation handling, rotation rollout, and an audit record for every trust-root change.

Effort. 8h

Reference.

| Inspected locations | Verification result | Revision |
|---|---|---|
| `services/review/attestation.py` | Not started. Still the platform `HmacAttestor`; no `rebar.attest` SSHSIG integration. | 641d605 |

#### Acceptance-gate live validation

Current state. Wired but unvalidated. The acceptance gate reuses the code-review verification contracts under the multi-pass kernel.
Target. A live run confirms the verification attributes score completion criteria adequately, or the contract is adjusted until they do.

Subtasks.
- Run the acceptance gate live over a fixed ticket set.
- Assess whether the code-review verification attributes score completion criteria adequately.
- Adjust the acceptance contract if the scoring is inadequate.

Effort. 3-6h, medium confidence

Reference.

| Inspected locations | Verification result | Revision |
|---|---|---|
| `services/review/kernel.py` | Wired, unvalidated. The four-pass kernel drives the acceptance stage; no live confirmation recorded. | 641d605 |

#### Authored-identity signing

Current state. Not started. No rebar authorship integration exists. Contingent on the object-store detaching authorship from the git store.
Target. Each authoring agent signs the plan and code change, verified at a merge gate, or a recorded finding of what still couples the identity system to the git store.

Subtasks.
- Evaluate whether the S3-backed rebar ticket store detaches rebar authorship from the git store.
- If detached, give each authoring agent a signing identity and sign the plan and code change, verified at a merge gate, with a trust root and key-rotation story.
- If not detached, record the residual coupling and the decision.

Effort. 8-16h, low confidence

Reference.

| Inspected locations | Verification result | Revision |
|---|---|---|
| none yet | Not started. No `rebar.authorship` usage; contingent on the object-store detaching authorship from the git store, unevaluated. | 641d605 |

#### Pluggable ticket-tracker and source-control backends

Current state. Not started. The ticket source is Jira-only through the adapter factory and per-project resolver, the source-control target is GitHub-only through `RealGitHost` and the `remote_url_for` seam, ticket write-back is Jira-specific, and the terminal signal is the GitHub pull-request-closed webhook.
Target. Two interoperating configurable backends. A ticket tracker defaulting to Jira with a rebar ticket-store option, and a source-control target defaulting to GitHub with a local git-folder option, so any supported combination resolves its read, write-back, approval, and terminal signal through the configured backends. A deployment that sets nothing behaves exactly as today.

Subtasks.
- Generalize the ticket-source seam behind the Jira adapter factory into a configurable ticket backend defaulting to Jira, and add the rebar ticket store, a git orphan branch synced to a private S3 bucket via `git-remote-s3`, as the second backend.
- Isolate the rebar ticket store on its own bucket or prefix with distinct IAM roles for read-only polling and for writes, deny cross-use by the evidence and audit writers, and include actor and run provenance on tickets-branch ref writes.
- Resolve the platform's logical lifecycle stages to the rebar store's own statuses for read, status update, and completion.
- Generalize the source-control seam behind `RealGitHost` and `remote_url_for` into a configurable backend defaulting to GitHub, and add a local git-folder backend that commits locally with no push and opens no pull request.
- Define a provider-neutral publish result carrying only source-control artifacts, the branch, summary path, patch path, and completion-signal id, that every source-control backend fills, and generalize pull-request discovery and reuse to a backend-neutral check, in local mode an existing branch that reached draft or merged status. Approval artifacts and status transitions are emitted by the ticket backend and folded into the workflow output after publish.
- Route all ticket write-back, the linkback, the status transitions, and the approval recording, through the configured ticket backend rather than Jira-specific calls, under a write contract with per-project credentials, ticket-ownership validation against the run, least-privilege write scopes, idempotent concurrency, and audit records naming actor, backend, project, ticket, and run id.
- For the local source-control backend, leave the promoted branch and a summary and patch artifact instead of opening a pull request, and replace the pull-request-closed webhook with an operator done command that authenticates an authorized operator, verifies the branch and artifact identifiers, emits an audit event, and behaves idempotently.
- Complete a run from a merged or closed ticket status only after authenticated status provenance, a matching publish result, passed gates, and verified branch and artifact identifiers, and record which terminal source wins when ticket status and source-control state disagree.
- Add one flag-driven `inv` command that polls the rebar tickets branch, since rebar emits no update webhook, reporting new tickets, updated fields such as description and acceptance criteria, and status changes, with flags to select which to report. This poll is the rebar backend's change-detection mechanism, its read contract, the counterpart to Jira's webhook and poller. Run it under an authenticated ticket-backend reader identity restricted to approved roles, redact non-allowlisted fields by default, and audit every invocation. Give it a lossless cursor contract, reading one branch-head snapshot against a persisted watermark, emitting ticket revision and branch commit identifiers, applying idempotency before side effects, and advancing the watermark only after intake or terminal signaling succeeds. Intake consumes it to start runs from new or updated allowlisted tickets, and the human-approval step consumes the status-change output. Human authoring and inspection of tickets without Jira use rebar's native CLI.
- Define the two backends as orthogonal contracts that compose, so combinations need no per-pair enumeration. The ticket backend owns read source, write-back, linkback, approval recording, and status and terminal updates. The source-control backend owns the publish artifact and the completion signal. The single cross-point is mapping the source-control completion signal onto the ticket terminal update, with the precedence rule above when the two disagree.

Behavior. With nothing configured, runs read and write through the Jira adapter and open a GitHub draft pull request exactly as today. Any combination of ticket backend and source-control backend composes from the two orthogonal contracts, resolving its linkback, approval, and terminal signal through the configured backends. With the rebar store configured, a status change writes to the rebar store, and seeding the store is rebar's own process outside this feature.

Effort. 32-48h, low confidence

Reference.

| Inspected locations | Verification result | Revision |
|---|---|---|
| `services/integration/jira/adapter.py` and the adapter factory and ticket-source seam, `services/integration/github`, `RealGitHost`, `remote_url_for`, `services/orchestration/temporal_workflows/draft_pull_request.py`; installed `nava-rebar` 0.12.0 `_commands/init.py`, `_store/s3_doctor.py` | Not started. Tickets Jira-only; source-control GitHub-only. rebar store is a git orphan branch (`tickets`) synced to S3 as per-ref bundles via `git-remote-s3`. | 641d605 |

#### Local-to-AWS mapping document

Current state. Not started as a committed artifact. The mapping exists only as plan prose.
Target. A committed document maps each local component to its AWS target.

Subtasks.
- Write the committed document mapping each local component to its AWS target.

Effort. 3h

Reference.

| Inspected locations | Verification result | Revision |
|---|---|---|
| `.agent-context/agentic_plan_v2.md` section 20 | Not started as a committed artifact. Mapping exists only as plan prose. | 641d605 |

#### Audit and evidence schema contracts

Current state. Not started. `schemas/audit_events` and `schemas/evidence` hold only `.gitkeep`.
Target. Committed JSON-schema contracts define the audit-event and evidence object shapes.

Subtasks.
- Define and commit the audit-event JSON schema.
- Define and commit the evidence-object JSON schema.

Effort. 4h

Reference.

| Inspected locations | Verification result | Revision |
|---|---|---|
| `schemas/audit_events`, `schemas/evidence` | Not started. Both directories hold only `.gitkeep`. | 641d605 |

#### S3-backed WORM evidence and audit store

Current state. Not started. Evidence persists only to Postgres and the sink location field is unused.
Target. Evidence and audit objects persist to the S3 and MinIO object store with a WORM retention model exercised locally before the AWS Object Lock mapping.

Subtasks.
- Implement an object-store evidence and audit sink over the S3 and MinIO store.
- Add the WORM retention model exercised locally.
- Wire the AWS Object Lock mapping.
- Enforce TLS, KMS-managed encryption, a least-privilege bucket policy, Object Lock retention governance, and audit logging on the object store.

Effort. 10h

Reference.

| Inspected locations | Verification result | Revision |
|---|---|---|
| `services/review/evidence.py` | Not started. `PostgresEvidenceSink` only; the `location` field is unused; no object-store sink. | 641d605 |

#### FedRAMP Medium hardening design

Current state. Not started as a committed artifact. Only plan prose exists.
Target. A committed hardening design ties each component to its FedRAMP Medium controls.

Subtasks.
- Write the hardening design covering the Temporal control plane, edge endpoints, data, secrets, identity, images, audit export, and webhook relay policy, each tied to its control family.

Effort. 8h

Reference.

| Inspected locations | Verification result | Revision |
|---|---|---|
| none yet | Not started as a committed artifact. Only plan prose exists. | 641d605 |

#### Evidence retention configuration

Current state. Partial. A retention column exists on the evidence record but no configuration value sets it.
Target. A configuration value sets the retention period, defaulting to the FedRAMP Moderate baseline.

Subtasks.
- Add a configuration value that sets the retention period, defaulting to the FedRAMP Moderate baseline, and wire it into evidence records.

Effort. 2h

Reference.

| Inspected locations | Verification result | Revision |
|---|---|---|
| `services/review/evidence.py`, `services/integration/mcp_gateway/evidence_reader.py` | Partial. A retention column exists on the evidence record; no config value sets it. | 641d605 |

#### Private-endpoint jump-host container image

Current state. Not started. Only references to the existing external jump-host exist.
Target. A deployable container image terminates the developer forward inside a target VPC so a laptop reaches the private Bedrock endpoint through it.

Subtasks.
- Build a deployable jump-host container image that terminates the developer forward inside a target VPC.
- Wire an existing connectivity provider to forward through it.

Effort. 6h

Reference.

| Inspected locations | Verification result | Revision |
|---|---|---|
| `scripts/config.py` connectivity settings | Not started. Only references to the existing external jump-host; no container image. | 641d605 |

#### Repository-intelligence production server boundary

Current state. Not started. The service runs `ThreadingHTTPServer` with no error boundary, no readiness probe, and sequential providers.
Target. The context-package endpoint serves from a WSGI application under gunicorn with a handler error boundary, liveness and readiness probes, and bounded concurrent provider fan-out.

Subtasks.
- Replace `ThreadingHTTPServer` with a WSGI application under gunicorn, preserving routes and status codes.
- Add a handler error boundary returning a readable 500.
- Add liveness and readiness probes and wire them.
- Run the independent providers through a bounded concurrent fan-out with per-provider timeouts and fallbacks.

Effort. 12h

Reference.

| Inspected locations | Verification result | Revision |
|---|---|---|
| `services/repository_intelligence/server.py`, `services/repository_intelligence/context_builder/context_service.py` | Not started. `ThreadingHTTPServer` with no error boundary or readiness probe; providers run sequentially. | 641d605 |

#### Full evidence artifact coverage

Current state. Partial. The sink stores review verdicts, scanner findings, and gate results. Plans, pull-request summaries, approvals, prompt references, and build and test reports are not routed through it.
Target. Every artifact type persists through the content-addressed evidence sink and is retrievable by id.

Subtasks.
- Route plans, pull-request summaries, approvals, prompt references, and build and test reports through the content-addressed evidence sink.
- Confirm each is retrievable by id through the reader.

Effort. 6h

Reference.

| Inspected locations | Verification result | Revision |
|---|---|---|
| `services/review/evidence.py`, `services/integration/mcp_gateway/evidence_reader.py` | Partial. Sink stores review verdicts, scanner findings, gate results; plans, PR summaries, approvals, prompt references, and build and test reports are not routed. | 641d605 |

#### Remaining connectivity and sandbox confirmations

Current state. Partial. The jump-host and DNS path and the local profile credential selection are confirmed. The deployed-workload credential method, the OpenHands sandbox design, and the integration-format confirmations remain.
Target. Each remaining item is confirmed and reflected in config and adapters.

Subtasks.
- Confirm the local credential method for deployed workloads.
- Confirm the OpenHands sandbox design.
- Confirm the Jira, GHES, and security-tool integration formats and reflect them in config and adapters.

Effort. 4-9h, low confidence

Reference.

| Inspected locations | Verification result | Revision |
|---|---|---|
| `scripts/config.py`, integration adapters | Partial. Jump-host and DNS path and `aws_profile` confirmed; deployed-workload credential method, OpenHands sandbox design, and integration-format confirmations open. | 641d605 |

#### Prompt centralization

Current state. Not started. The seven prompts are Python constants in `services/prompts/library.py` and `config/prompts/` holds only `.gitkeep`.
Target. The prompt library loads from `config/prompts/`, the Langfuse seed publishes from the same files, and no prompt text remains in code.

Subtasks.
- Move the prompt definitions out of the code library into files under `config/prompts/`.
- Repoint the loader and the Langfuse seed to read from those files.
- Remove the prompt text from code.

Effort. 4h

Reference.

| Inspected locations | Verification result | Revision |
|---|---|---|
| `services/prompts/library.py`, `config/prompts/` | Not started. Prompts are Python constants; `config/prompts/` holds only `.gitkeep`. | 641d605 |

#### Database seeds and reset

Current state. Not started. `database/seeds/` and `database/reset/` hold only `.gitkeep`.
Target. `inv seed` loads allowlisted fixtures and `inv db-reset` restores a known state, both from committed files and both idempotent.

Subtasks.
- Add allowlisted seed fixtures for the demo project and repositories.
- Add an idempotent reset routine that restores a known state.
- Wire `inv seed` and `inv db-reset` to the committed files.

Effort. 5h

Reference.

| Inspected locations | Verification result | Revision |
|---|---|---|
| `database/seeds/`, `database/reset/`, `tasks.py` | Not started. Both directories hold only `.gitkeep`. | 641d605 |

#### JavaScript SCIP verification corpus

Current state. Not started. No JavaScript-only demo repository, gold set, or scorecard exists.
Target. A committed JavaScript-only demo repository, gold set, and acceptance scorecard prove the SCIP and router read path with the inferred-config indexer.

Subtasks.
- Build a dependency-free JavaScript-only demo repository with no `tsconfig.json`.
- Seed, gold-tag, and allowlist it.
- Generate the JavaScript gold set and acceptance scorecard with the SCIP and router providers gated.

Effort. 6h

Reference.

| Inspected locations | Verification result | Revision |
|---|---|---|
| `services/repository_intelligence/eval/gold`, `services/repository_intelligence/eval/scorecards`, allowlist config | Not started. No JavaScript-only demo repository, gold set, or scorecard; six language corpora exist, JavaScript absent. | 641d605 |

#### Snapshot-drift handling

Current state. Not started. No drift suspend reason, no debounced drift checkpoint, no drift decision signal. The build plan is written.
Target. A material Jira change during a run pauses at a debounced drift checkpoint, a human continues, restarts, or abandons, a restart reuses the pull request with clean-or-incremental planning, and edit bursts coalesce to the latest snapshot.

Subtasks.
- Add the debounced webhook-triggered drift checkpoint reusing the webhook front door.
- Add the material-field classifier.
- Add the drift suspend reason and park at the next of three checkpoints.
- Add the three-choice human decision signal and operator command.
- Add the two-pass restart planning with defunct-code verification and the clean-re-implementation default.

Effort. 14h

Reference.

| Inspected locations | Verification result | Revision |
|---|---|---|
| `services/orchestration/suspension.py`, `services/orchestration/webhook`, `.agent-context/phase-17-build-plan.md` | Not started. No `AWAITING_SNAPSHOT_DRIFT` suspend reason, checkpoint, or decision signal; build plan written. | 641d605 |

#### Streaming inference and interactive surfaces

Current state. Not started. No per-logical-name streaming flag and no streaming consumer.
Target. An interactive surface renders a streamed Bedrock response through the proxy and the per-call accounting matches the non-streamed path.

Subtasks.
- Add a per-logical-name streaming flag to the model gateway.
- Build a streaming consumer on an interactive surface.
- Confirm token and cost accounting matches the non-streamed path.

Effort. 6h

Reference.

| Inspected locations | Verification result | Revision |
|---|---|---|
| `services/model_gateway`, LiteLLM config | Not started. No per-logical-name streaming flag; no streaming consumer. | 641d605 |

#### Single-turn finder tuning

Current state. Not started. A single-turn path exists in the kernel but is selected only by presence of a repository-intelligence root, with no A/B result.
Target. A recorded A/B result over a fixed ticket set, and either the finder switched to single-turn with find quality held or a recorded decision to keep it agentic.

Subtasks.
- Build the finder A/B harness over a fixed ticket set.
- Run and record the A/B result.
- Switch the finder to single-turn if find quality holds, or record the decision to keep it agentic with the measured reason.

Effort. 4-8h, medium confidence

Reference.

| Inspected locations | Verification result | Revision |
|---|---|---|
| `services/review/kernel.py` | Not started. A single-turn path exists, selected only by presence of a repository-intelligence root; no A/B result or recorded decision. | 641d605 |

### Feature Relationships

<!-- ce-section: work-relationships -->

Order is not implied by this list. These are the factual dependencies a version-grouping document will need.

- Authored-identity signing depends on the shared S3 and MinIO object store, which the pluggable ticket-tracker and source-control backends feature and the S3-backed WORM evidence and audit store both use.
- The rebar ticket backend and the S3-backed WORM evidence and audit store share the same S3 and MinIO object store, so standing up that store serves both.
- The audit and evidence schema contracts and the S3-backed WORM evidence and audit store underpin the production and FedRAMP work.
- Snapshot-drift handling reuses the webhook front door, the human-gate park machinery, and the plan, budget, and repository-intelligence pinning already delivered.

### Dependencies and Assumptions

- The rebar ticket store from rebar v0.12.0 is available and is the rebar ticket backend in the pluggable ticket-tracker and source-control backends feature.
- The S3 and MinIO object store introduced for the observability stack is the shared store for the rebar ticket backend, the WORM evidence and audit store, and the audit and evidence path.
- rebar 0.13 is the external dependency for the two held items and its timing is not controlled here.
- The ticket-source seam behind the Jira adapter factory and the `remote_url_for` seam behind `RealGitHost` are the extension points for the pluggable ticket-tracker and source-control backends feature.

### Outstanding Questions

Deferred to planning.
- rebar ticket-update detection. rebar emits no webhook, so detecting a ticket status change means polling the tickets branch through an `inv` command. Confirm the polling cadence and how it integrates with the webhook front door during planning.
- Multi-project convergence. Whether to converge the per-project credential and board resolvers onto rebar's multi-project support, keep the platform resolvers, or bridge the two. Flagged by the maintainer for discussion.
- Reconciler and store roadmap. Which previously declined maintainer roadmap items the platform now adopts. Partly resolved by the pluggable ticket-tracker and source-control backends feature adopting the rebar ticket store.

### Held, blocked on external rebar 0.13

- Dropping the process-wide httpx transport header workaround in favor of a native rebar request-header seam. The workaround is in place and attribution is intact.
- Migrating the planner from its raw httpx LiteLLM client onto the rebar runner. This zeroes the in-flight token counts the budget path needs until the rebar usage fix ships. The hermetic auth-header consolidation slice is already delivered.

Unblock rule. Before converting either held item into a feature, verify that rebar 0.13 ships the request-header seam and the OpenAI-path usage fix.

### Not goals

- Re-verifying the platform foundation, repository intelligence, ticket and coordination, and enterprise adapters and coding line by line. They are accepted as delivered and are transitively exercised by the verified downstream pipeline.
- Reversing the Jira-canonical stance. The ticket tracker adds a backend and keeps Jira the default.
- Reintroducing platform-managed Jenkins. It was removed and project-run checks in an ephemeral pod replace it.

### Sources and verification

Verification method. Each feature was checked against the committed code and tests, not against plan prose or commit messages. The `.agent-context/agentic_plan_v2.md` section 19 annotations are stale for the later work, so they were treated as claims to confirm. Verification ran against `main` at revision `641d605`. The current head `a8a5f18` adds only a test stub and does not change any verified state below.

Per-feature inspected locations, verification results, and the revision are recorded in the Reference table under each feature above.
