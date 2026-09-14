# Changelog

All notable changes to this platform are recorded here. The format follows Keep a Changelog, and the platform aims to follow semantic versioning once releases are tagged.

## 0.2.0 (unreleased)

rebar review integration. The review gates run on the native Bedrock path with per-leg failover, every review verdict is signed with an asymmetric signature verified against a public trust root, and each authoring agent signs its plan and code change under its own rebar identity for a merge gate to verify.

### Added

- Native Bedrock review path. The rebar review gates run reviews natively on Amazon Bedrock through a dedicated review worker, resolving each tier to a Claude primary with a Nova failover, routing through rebar's runner so prompt caching stays enabled, pricing review cost cache-aware so cached tokens are not billed at the input rate, and exporting native generations to Langfuse with the execution id and stage stamped onto each span.
- Signed review verdicts across every review stage. Plan review, code review, security review, and acceptance verification sign the canonical verdict payload with an SSHSIG signature carried in a DSSE envelope, and the gate verifies each signature against a public trust root before it accepts the verdict. A verifier holds only public keys, so it cannot forge a verdict. The evidence record carries the signing scheme, the signer key identifier, and the envelope, and enough detail to re-verify the verdict offline.
- A versioned trust-root bundle of public signer keys with per-key identifiers and validity windows, revocation of a compromised key, and an overlapping rotation window that admits a new key before the old key retires.
- A review-worker signing preflight that fails the worker at startup when signing is enabled without a usable key, a loadable trust root, and `ssh-keygen`, so a misconfiguration surfaces at startup rather than on the first review.
- A trust-root change audit. The review worker records a `review_trust_root_change` event to the audit store at startup when the loaded bundle differs from the last recorded one, naming the actor, the change summary, and the affected key identifiers.
- Authored-identity signing on the rebar backend. Each authoring agent signs the plan it produces and the code change it commits under its own rebar identity, and a merge gate verifies both signatures against the identity trust root and fails closed on an absent, tampered, or out-of-trust-root signature. The identity keyring supports trust-on-first-use genesis, signed key rotation with an overlapping window, revocation, and a keyring-change audit record. A Jira deployment has no rebar authorship substrate and does not participate.
- `REVIEW_SIGNING_ENABLED`, `REVIEW_SIGNING_KEY_PATH`, and `REVIEW_TRUST_ROOT_PATH` settings, with signing on by default, and `openssh-client` in the runtime image so `ssh-keygen` is available.
- `AUTHORSHIP_SIGNING_ENABLED`, `AUTHORSHIP_IDENTITY_ID`, and `AUTHORSHIP_SIGNING_KEY_PATH` settings for authored-identity signing.
- An acceptance-gate validation harness and a labeled acceptance set. The set pairs each ticket's acceptance criteria and completion evidence with a met or unmet outcome, and the harness runs the acceptance gate over the set and scores each verdict as an agreement, a false accept, or a false block against a fixed adequacy bar. The live scored run over the configured review path produces a recorded scorecard.

### Changed

- Review verdict signing is on by default and fails closed. When signing is enabled but the key or trust root is not configured, a review stage raises rather than downgrading to the symmetric HMAC path. Setting `REVIEW_SIGNING_ENABLED` to false selects the HMAC path for local development.
- Database migrations apply only through the `db-migrate` container. `inv db-migrate` and `inv db-reset` run that container, which mounts the working-tree migrations so the applied scripts match the checkout, and a guard in the migration environment refuses `alembic upgrade`, `downgrade`, and `stamp` on the host so a host run cannot advance the database past the revisions the container carries.

## 0.1.0 (2026-09-11)

First versioned release of the platform. This version makes the ticket tracker and the source-control target pluggable behind two orthogonal contracts, bounds concurrent runs with a platform admission cap, and re-grounds a run that discovers a cross-ticket dependency during planning on both the rebar and Jira backends. A deployment that configures neither backend behaves exactly as before.

### Added

- `TicketBackend` contract and a `build_ticket_backend` factory that defaults to Jira, threaded through every orchestration call site.
- A rebar ticket-store backend that reads tickets, transitions status, records linkback and approval, and resolves the platform's logical lifecycle stages to the store's own statuses.
- A `build_git_host` selection defaulting to GitHub and a `LocalGitHost` that commits locally, performs no push, and opens no pull request, leaving a promoted branch and a summary and patch artifact.
- A provider-neutral `PublishResult` carrying source-control artifacts only, with backend-neutral publish discovery.
- An operator done command for the local source-control backend, authenticated, idempotent, and audited.
- A flag-driven `inv` poll over the rebar tickets branch with a lossless cursor, an authenticated reader identity, field redaction, and per-invocation auditing.
- Rebar ticket-store credential isolation config that separates read-only polling and write roles from the evidence and audit writers.
- A `tickets` per-environment config file, version-gated like the other config files.
- Reusable `vpc-endpoints`, `ecr`, `cloudtrail`, and `bedrock-iam` Terraform modules that own the shared VPC endpoints, the container registry, the audit trail, and the `bedrock:InvokeModel` policy, each toggleable so an account reuses infrastructure it already has.
- A `bedrock-jumphost-fargate` Terraform module that provisions an ephemeral ECS Exec hop to the private Bedrock endpoint, with least-privilege task roles, a KMS-encrypted and audited exec channel, and a hardened exec-log bucket, consuming the shared endpoints and registry as inputs.
- A `private-bedrock` composition root that wires the network, endpoints, registry, audit trail, and jumphost into one apply, working from zero or against an existing VPC through tfvars, with greenfield and existing-network examples.
- A `jumphost-base` container image and an `inv publish-jumphost-image` task that builds and pushes it to the account ECR repository.
- An execution orphan reconciler that runs at the start of every intake pass and returns a non-terminal execution to a terminal state when its workflow is missing or closed, so the platform concurrency cap reclaims slots held by runs whose workflow no longer exists. The grace window is configurable through `EXECUTION_ORPHAN_GRACE_SECONDS`.
- Cross-ticket dependency discovery. When `DEPENDENCY_DETECTION_ENABLED` is set, a run detects during planning whether its change depends on another in-flight run through file-impact overlap and unresolved-symbol references, records the dependency on the ticket store, pauses on the dependency seam without holding compute, and resumes when the blocking ticket reaches certified terminal completion. On resume it re-captures its base and re-plans, then re-verifies against the runs still in flight, bounded by a re-plan attempt cap and a dependency-cycle guard. Detection is inactive for a backend without the file-impact capability, and the attempt cap is configurable through `DEPENDENCY_REPLAN_ATTEMPTS`.
- Backend-neutral cross-ticket dependency discovery. The per-run predicted touch set moves to a platform-owned `run_file_impact` table read through a `FileImpactStore`, so overlap detection no longer depends on the ticket backend. The `DependencyBackend` capability narrows to the graph operations `link`, `declared_dependencies`, and `dependents`, plus a `dependency_graph_active` predicate, and the Jira adapter implements it over Jira issue links. A run publishes its touch set on any backend and detects, records, and pauses only when its backend reports the graph active. A Jira project activates the graph by configuring `dependency_link_type` with an existing Jira link type; a project that leaves it unset publishes its touch set but records nothing and pauses on nothing.

### Changed

- The implementation token budget counts billable (non-cached) tokens rather than total tokens, so prompt-cache reads no longer count against the cap. A run that reuses a large cached context is bounded by cost and call-count caps instead of tripping the token cap on cache reads.
- The security scan and source-control mediators guard against the backend-neutral source-control allowlist rather than the GitHub credential file, so a deployment targeting a local git folder admits its repositories without a `github.yaml`.
- The jumphost connect script drives the Fargate run-task and ECS Exec port-forward flow, resolving the cluster and task definition from SSM parameters, and preserves the `-n`, `-e`, `-p` interface the connectivity provider calls.
- Ticket write-back routes through the configured ticket backend under a write contract with per-project credentials, ticket-ownership validation, least-privilege scopes, idempotent concurrency, and an audit record naming actor, backend, project, ticket, and run id.
- Terminal completion from a merged or closed ticket status proceeds only after authenticated status provenance, a matching publish result, passed gates, and verified branch and artifact identifiers, with a recorded precedence rule when ticket status and source-control state disagree.

### Fixed

- Planning-stage context retrieval derives symbol and file focus terms from the ticket title and description instead of sending the natural-language title as the focus. The repository-intelligence providers key on a symbol or file name, so the title matched nothing and the context package returned no impacted files, symbols, or references. The context service now extracts code-like identifiers and repository file paths from the title and description, queries the index for each, and unions the results, so the predicted touch set reflects the files a run is expected to touch and cross-ticket dependency detection operates on populated data. A single-token focus with no description keeps the prior single-symbol behavior.
- The pre-PR issue recheck normalizes the fresh ticket through the same backend that produced the stored intake payload, so a rebar run no longer pauses with a false execution-invalidating verdict from comparing a rebar-shaped stored payload against a Jira-normalized fresh payload. Each ticket backend owns a `recheck_payload` method that returns the comparison payload in its intake shape.
- The promotion policy reads its project and repository allowlists and its CI requirement from the decision input rather than hardcoded sets, and the policy mediator fills those from the configured ticket and source-control backends. A local source-control run whose backend runs no continuous integration is no longer denied for `ci_not_successful`, `repository_not_allowlisted`, or `jira_project_not_allowlisted`, while a deployment that supplies no input allowlists keeps the previous defaults.
- Intake starts a local source-control run with the await-close step enabled so the run parks at `AWAITING_PR_CLOSE` until the operator done command signals completion, which transitions the ticket to its terminal status. Without this a local run stopped at branch promotion and its ticket never reached the closed status.
