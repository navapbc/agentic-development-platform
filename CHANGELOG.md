# Changelog

All notable changes to this platform are recorded here. The format follows Keep a Changelog, and the platform aims to follow semantic versioning once releases are tagged.

## 0.1.0 (unreleased)

First versioned release of the platform. This version makes the ticket tracker and the source-control target pluggable behind two orthogonal contracts, and a deployment that configures neither behaves exactly as before.

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

### Changed

- The implementation token budget counts billable (non-cached) tokens rather than total tokens, so prompt-cache reads no longer count against the cap. A run that reuses a large cached context is bounded by cost and call-count caps instead of tripping the token cap on cache reads.
- The security scan and source-control mediators guard against the backend-neutral source-control allowlist rather than the GitHub credential file, so a deployment targeting a local git folder admits its repositories without a `github.yaml`.
- The jumphost connect script drives the Fargate run-task and ECS Exec port-forward flow, resolving the cluster and task definition from SSM parameters, and preserves the `-n`, `-e`, `-p` interface the connectivity provider calls.
- Ticket write-back routes through the configured ticket backend under a write contract with per-project credentials, ticket-ownership validation, least-privilege scopes, idempotent concurrency, and an audit record naming actor, backend, project, ticket, and run id.
- Terminal completion from a merged or closed ticket status proceeds only after authenticated status provenance, a matching publish result, passed gates, and verified branch and artifact identifiers, with a recorded precedence rule when ticket status and source-control state disagree.

### Fixed

- The pre-PR issue recheck normalizes the fresh ticket through the same backend that produced the stored intake payload, so a rebar run no longer pauses with a false execution-invalidating verdict from comparing a rebar-shaped stored payload against a Jira-normalized fresh payload. Each ticket backend owns a `recheck_payload` method that returns the comparison payload in its intake shape.
- The promotion policy reads its project and repository allowlists and its CI requirement from the decision input rather than hardcoded sets, and the policy mediator fills those from the configured ticket and source-control backends. A local source-control run whose backend runs no continuous integration is no longer denied for `ci_not_successful`, `repository_not_allowlisted`, or `jira_project_not_allowlisted`, while a deployment that supplies no input allowlists keeps the previous defaults.
- Intake starts a local source-control run with the await-close step enabled so the run parks at `AWAITING_PR_CLOSE` until the operator done command signals completion, which transitions the ticket to its terminal status. Without this a local run stopped at branch promotion and its ticket never reached the closed status.
