---
title: Authored-Identity Signing - Plan
type: feat
date: 2026-09-02
origin: docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-brainstorm
execution: code
---

# Authored-Identity Signing - Plan

## Goal Capsule

Objective. A merge gate can prove which agent authored a plan and its code change, verified against that agent's public identity, or a recorded finding states exactly what still couples the identity system to the git store.

Means. Give each authoring agent an ed25519 identity, sign the plan and code change with rebar's authorship SSHSIG, and verify at the merge gate, after a discovery step confirms the v0.1.0 rebar ticket store resolves identity tickets (KTD1, KTD2, KTD3).

Product authority. Enriches the "Authored-identity signing" feature in the roadmap at [remaining-work roadmap plan](docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md) and its v0.2.0 grouping in [ROADMAP.md](ROADMAP.md). Design intent comes from `.agent-context/agentic_plan_v2.md` Phase 10 Option B and Phase 16.

Stop conditions. This feature depends on the v0.1.0 rebar ticket store. The first unit is a discovery step; if it finds the identity system cannot resolve identities from the platform's store, the feature delivers a recorded coupling finding instead of signing.

## Product Contract

Product Contract preservation. Sourced from the "Authored-identity signing" roadmap feature; requirements restate its subtasks with no scope change.

### Summary

Each authoring agent holds its own ed25519 identity. The platform signs the plan and the code change with rebar's authorship SSHSIG (`rebar.authorship.v1`), and a merge gate verifies the signature against the agent's identity before promotion. rebar's authorship resolves an agent's public keys from an identity ticket in the ticket store, so this feature depends on the v0.1.0 rebar ticket store resolving those tickets. A discovery step confirms that before building.

### Problem Frame

Nothing today proves which agent authored a change. rebar ships an authorship system that signs a payload with a per-agent ed25519 key and verifies it against the agent's identity ticket. Two of its verify paths resolve the trust root through `rebar.show_ticket`, and its era-validity check (`verify_authorship_at_commit`, the merge-gate path) reads git ancestry through the tracker directory. Because the v0.1.0 rebar ticket store is a git branch synced to S3, that git-ancestry requirement is plausibly satisfiable, but it must be confirmed against the platform's store before the feature commits to signing.

### Requirements

- R1. A discovery step determines whether the v0.1.0 rebar ticket store lets rebar's authorship system resolve identity tickets and verify era-validity at the merge gate. Its outcome gates the rest.
- R2. If discovery holds, each authoring agent has an ed25519 identity registered as an identity ticket carrying its public keys.
- R3. If discovery holds, the platform signs the plan and the code change with rebar's authorship SSHSIG under the `rebar.authorship.v1` namespace, keyed by the agent's identity.
- R4. If discovery holds, a merge gate verifies the per-agent signature against the run's expected `agent_id` and fails closed when the signature is absent, forged, from the wrong agent, from an out-of-era key, from a revoked key, or when verification is unconfigured.
- R5. If discovery does not hold, the feature records a finding naming exactly what still couples the identity system to the git store and the decision taken, and delivers no partial signing path.
- R6. Per-agent private keys are held by a signing broker service, not by agent workers. An agent authenticates to the broker and never holds a private key. Keys carry a documented rotation and revocation story, and the broker image carries `ssh-keygen` 8.9 or newer.

### Scope Boundaries

- Signed review verdicts (gate attestation) is a separate feature; this feature signs authorship, not verdicts.
- No new ticket store; this consumes the v0.1.0 rebar store.
- The agent identity is the existing `agent_id`; this feature does not redesign agent identity, only binds a key to it.

### Outstanding Questions

- Resolve before planning. None. R1 is the discovery gate and is resolved in U1 before the signing units proceed.
- Deferred to planning. Resolved here: era-validity uses `verify_authorship_at_commit` against the S3-synced tickets worktree. If git ancestry is unavailable, discovery (U1) routes to the coupling finding (U5) rather than relaxing the era check.

### Sources

- rebar authorship (installed `nava-rebar` 0.12.0): `rebar/attest/authorship.py` (`sign_authorship(payload, key_path, principal)`, `verify_authorship(envelope, identity_id, repo_root)`, `verify_authorship_at_commit(...)`, `verify_authorship_any_key(...)`, `resolve_trust_root(identity_id, repo_root)`, `allowed_signers_from_keys`, `AUTHORSHIP_KIND`/`AUTHORSHIP_NAMESPACE` `rebar.authorship.v1`), `rebar/attest/sshsig.py`, `rebar/attest/registry.py`.
- Ticket-store coupling: `rebar.show_ticket(identity_id, repo_root)` resolves the identity ticket's `keys`; `verify_authorship_at_commit` reads git ancestry through `rebar._commands._seam.tracker_dir`.
- v0.1.0 dependency: the rebar ticket store from [pluggable backends plan](docs/plans/2026-09-02-2022-feat-pluggable-ticket-source-control-backends-plan.md), a git orphan branch synced to S3.
- Agent identity: `services/orchestration/temporal_workflows/implementation.py` (`ImplementationInput.agent_id`, `ClaimRequest.agent_id`).
- Merge gate insertion: `services/orchestration/temporal_workflows/draft_pull_request.py` (promotion and terminal path), `services/orchestration/temporal_activities/pull_request.py` (`promote_pr`, `PromotePrRequest`).

## Planning Contract

### Key Technical Decisions

- KTD1. Gate the feature on a discovery unit that confirms rebar's authorship can resolve identity tickets and era-validity from the v0.1.0 store before any signing is built. Governs R1, R5. Rationale: `resolve_trust_root` and `verify_authorship` call `rebar.show_ticket`, and `verify_authorship_at_commit` needs git ancestry, so the store's shape decides feasibility.
- KTD2. Use rebar's authorship API directly (`sign_authorship`/`verify_authorship`/`verify_authorship_at_commit`) rather than reimplementing SSHSIG. Governs R3, R4. Rationale: the substrate exists and pins the `rebar.authorship.v1` scheme and namespace.
- KTD3. Bind the signing principal to the existing `agent_id` and register each agent's public keys as an identity ticket. Governs R2, R3. Rationale: `agent_id` already flows through `ImplementationInput` and `ClaimRequest`, so it is the natural authorship principal.
- KTD4. Verify at the merge gate in `promote_pr` with `verify_authorship_at_commit`, bind the accepted signature to the run's expected `agent_id`, and fail closed on absent, forged, wrong-agent, out-of-era, or revoked signatures and when verification is unconfigured. Governs R4. Rationale: `promote_pr` is the terminal step before a change lands, and a relaxed or unconfigured gate would let an unverified change promote.
- KTD5. Hold per-agent private keys in a signing broker service that signs on behalf of an authenticated agent; agent workers never hold private keys. Governs R6. Rationale: a `0600` file does not isolate agents that share a worker identity, so a broker that authenticates the caller and signs with only that agent's key is the isolation boundary.

### High-Level Design

Discovery first, then identity registration, signing, and a merge-gate verification, all contingent on discovery.

- Discovery. Confirm `rebar.show_ticket` resolves an identity ticket from the v0.1.0 store and that `verify_authorship_at_commit` reaches git ancestry through the S3-synced tickets worktree. Record the result. If it fails, deliver the coupling finding and stop.
- Identity. Register each authoring agent as an identity ticket carrying its ed25519 public keys, keyed by `agent_id`.
- Signing. At plan approval and at code completion, sign the plan text and the code change with `sign_authorship` under the agent's key.
- Merge gate. In `promote_pr`, verify the authorship signature with `verify_authorship_at_commit` (or `verify_authorship` if era-validity is relaxed by discovery), failing closed on absent, forged, or out-of-era signatures.

### Assumptions

- The v0.1.0 rebar ticket store is available and is a git branch synced to S3, so git ancestry exists for era-validity.
- rebar 0.12.0's `rebar/attest/authorship.py` works as installed, verified present.
- `ssh-keygen` 8.9+ and a `0600` per-agent key can be provisioned into the signing worker.

### Sequencing

U1 (discovery) comes first and gates everything. U2 (identity registration), U3 (signing), and U4 (merge-gate verification) proceed only if U1 holds; U3 depends on U2 and U4 depends on U3. If U1 fails, U5 (coupling finding) is the terminal unit instead.

## Implementation Units

### U1. Authorship-store discovery

Goal. Determine whether the v0.1.0 rebar ticket store lets rebar's authorship resolve identity tickets and era-validity.
Requirements. R1.
Files. `scripts/authorship_discovery.py` (a probe), a recorded finding under `docs/`.
Approach. Against the v0.1.0 store, register a throwaway identity ticket, call `rebar.show_ticket` to confirm resolution, sign a sample payload with `sign_authorship`, and verify with both `verify_authorship` and `verify_authorship_at_commit` to confirm era-validity reaches git ancestry through the S3-synced tickets worktree (KTD1). Record the outcome and, if era-validity is unavailable, whether a relaxed rule is acceptable. Resolve the era-validity Outstanding Question here.
Test scenarios. The probe registers and resolves an identity ticket. `verify_authorship` verifies a signed sample. `verify_authorship_at_commit` reaches git ancestry or reports the exact missing coupling. The outcome is recorded as a committed finding.
Verification. The recorded discovery finding, `tests/unit/test_authorship_discovery.py` (new, over a fixture store), `inv test`.

### U2. Per-agent identities and signing broker

Goal. Register each authoring agent as an identity ticket carrying its ed25519 public keys, and hold the private keys in a signing broker that signs on behalf of an authenticated agent.
Requirements. R2, R6. Depends on U1 holding.
Files. `services/signing_broker/` (new broker service that holds the keys and exposes an authenticated sign call), `services/integration/tickets/` or `services/orchestration/` identity registration, `scripts/config.py` (broker endpoint and key store), `compose.yaml` (the broker service), deployment docs.
Approach. Provision a per-agent ed25519 key held by the broker, and register the public keys as a `rebar` identity ticket keyed by `agent_id` using the store's write path (KTD3, KTD5). Agent workers never hold a private key; they call the broker with an authenticated `agent_id` and the broker signs with that agent's key only. Document rotation and revocation.
Test scenarios. An agent's identity ticket resolves through `rebar.show_ticket` and carries its public keys. A rotated key updates the identity ticket keyring. A revoked key is removed from acceptance and fails closed at the merge gate. An agent worker holds no private key. The broker signs only with the authenticated caller's key, and a caller cannot obtain or sign with another agent's key. An unregistered agent has no identity ticket.
Verification. `tests/unit/test_agent_identity.py` (new), `tests/unit/test_signing_broker.py` (new), `inv test`.

### U3. Sign plan and code change

Goal. Sign the plan text and the code change with the authoring agent's authorship key.
Requirements. R3. Depends on U2.
Files. `services/orchestration/temporal_workflows/draft_pull_request.py` (sign at plan approval and code completion), `services/orchestration/temporal_activities/` (a signing activity that calls the broker), `services/signing_broker/` (the authenticated sign call).
Approach. At plan approval, sign the approved plan text; at code completion, sign the code change, both by calling the signing broker with the authenticated `agent_id`. The broker runs `sign_authorship(payload, key_path, principal=agent_id)` under `rebar.authorship.v1` with that agent's key (KTD2, KTD3, KTD5). Persist the authorship envelopes and an `AuthorshipEvidence` record, keyed by `execution_id` and carrying `agent_id`, the plan envelope id, the code envelope id, the source diff hash, the tracker commit, and the tracker position, through the evidence sink, so the merge gate can load them.
Test scenarios. An approved plan carries an authorship signature keyed by the agent. A code change carries an authorship signature keyed by the agent. The envelopes persist in evidence. A run with no provisioned key fails closed with a clear error rather than an unsigned change proceeding.
Verification. `tests/unit/test_authorship_signing.py` (new), `inv test`.

### U4. Merge-gate verification

Goal. Verify the per-agent signature at the merge gate and fail closed.
Requirements. R4. Depends on U3.
Files. `services/orchestration/temporal_activities/pull_request.py` (`promote_pr` loads `AuthorshipEvidence` and verifies authorship), `services/orchestration/temporal_workflows/draft_pull_request.py` (promotion path).
Approach. In `promote_pr`, load the `AuthorshipEvidence` for the execution and verify the plan and code signatures with `verify_authorship_at_commit` against the run's expected `agent_id`, the tracker commit, and the tracker position (KTD4). Fail closed when a signature is absent, forged, from the wrong agent, out-of-era, from a revoked key, or when verification is unconfigured, blocking promotion.
Test scenarios. A valid signature from the expected agent passes the merge gate. A forged signature fails closed. A valid signature from the wrong registered agent fails closed. An out-of-era key fails closed. A revoked key fails closed. An absent signature fails closed. Unconfigured authorship verification fails closed in promotion. A passing signature promotes normally.
Verification. `tests/unit/test_merge_gate_authorship.py` (new), `tests/integration/` promotion scenario (extend), `inv ci`.

### U5. Coupling finding (fires only if U1 fails)

Goal. If discovery fails, record what still couples the identity system to the git store and the decision.
Requirements. R5.
Files. a recorded finding under `docs/`.
Approach. Name the exact coupling that blocks authorship on the platform's store (for example era-validity requiring a git-tracker ancestry the store does not expose), and record the decision, defer, adopt a relaxed rule, or adopt the git store for identities alone. Deliver no partial signing path.
Test scenarios. The finding names the specific coupling and the decision. No signing code path is left half-built.
Verification. The recorded finding, `inv test` unaffected.

## Verification Contract

- Lint. `ruff check` clean. Types. `basedpyright` clean on the changed packages.
- Unit and end-to-end. `inv ci` green.
- Full suite. `inv test` green, including `tests/integration`.
- Discovery gate. U1's recorded finding decides whether U2 through U4 or U5 is the delivered path.
- Regression floor. Unconfigured authorship verification fails closed in promotion. Pre-existing promotion tests either provide fixture authorship signatures or run through the discovery-failed (U5) path.

## Definition of Done

- Global. Either R2 through R4 are met (discovery held) or R5 is met (discovery did not). R1 and R6 are met in both cases. `inv ci` and `inv test` are green. Abandoned code is removed from the diff.
- U1. A committed discovery finding states whether the v0.1.0 store supports authorship resolution and era-validity.
- U2. Each authoring agent has an ed25519 identity ticket; private keys are held by the signing broker, not agent workers; rotation and revocation are documented and tested; and a caller cannot sign with another agent's key.
- U3. Plans and code changes carry per-agent authorship signatures and an `AuthorshipEvidence` record persisted in evidence, failing closed when no key is provisioned.
- U4. The merge gate loads the `AuthorshipEvidence`, verifies against the expected `agent_id` with era-validity, and fails closed on absent, forged, wrong-agent, out-of-era, revoked, or unconfigured signatures.
- U5. If discovery failed, the coupling finding and decision are recorded and no partial signing path was shipped.
