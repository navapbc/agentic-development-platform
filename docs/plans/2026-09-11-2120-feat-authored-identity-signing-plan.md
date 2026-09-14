---
title: Authored-Identity Signing - Plan
type: feat
date: 2026-09-11
origin: docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-brainstorm
execution: code
---

# Authored-Identity Signing - Plan

## Goal Capsule

Objective. Each authoring agent signs the plan and the code change it produces under its own identity, a merge gate verifies those signatures against the identity's trust root and fails closed on an absent or invalid signature, and the residual coupling between rebar authorship and the git store is evaluated and recorded first.

Means. Evaluate whether the rebar ticket store detaches authorship from the git store and record the finding. Then, on the rebar backend, give each authoring agent a rebar identity with a keyring, sign the plan and code authorship through `rebar.attest.authorship` as that identity, and verify authorship at a merge gate through `verify_authorship_at_commit` against the identity trust root, with a key-rotation and revocation story.

Product authority. This plan enriches one feature from the roadmap at [2026-09-02-1801-feat-remaining-work-roadmap-plan.md](docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md), the "Authored-identity signing" entry. It does not widen that feature's scope.

Stop conditions. The evaluation in U1 gates the implementation. When the evaluation concludes signing is infeasible on the available store, the plan stops after recording the residual coupling and the decision. Authored-identity signing is a rebar-backend capability, and a Jira-only deployment does not participate.

## Product Contract

Product Contract preservation. Sourced from the roadmap feature "Authored-identity signing". The requirements below restate that feature's subtasks as R-IDs with no scope change.

### Summary

Today no authoring agent has a signing identity, and neither the plan nor the code change carries a signature that ties it to the agent that produced it. rebar 0.12.0 ships an authorship substrate the platform does not use. This plan first evaluates whether the rebar ticket store, including its S3-backed option, detaches authorship from the git store, because the roadmap made the feature contingent on that. The evaluation's grounded expectation is that authorship stays anchored to the ticket store's git structure, since identities are rebar tickets and authorship events resolve to commits, and the S3 option syncs that branch to a private bucket rather than detaching it. Because the v0.1.0 rebar backend keeps that store isolated on a dedicated orphan branch never pushed to a shared host, signing is nonetheless feasible on the rebar backend. The plan then gives each authoring agent a rebar identity with a keyring, signs the plan and code authorship as that identity, and verifies the signatures at a merge gate that fails closed. A Jira-only deployment, which has no rebar authorship substrate, does not participate, and that limitation is recorded.

### Problem Frame

`services/review/` signs review verdicts, but there is no signature that binds a plan or a code change to the agent that authored it, so a merge gate cannot attest authorship. rebar's `rebar.attest.authorship` module signs an event's authorship as an in-toto statement wrapped in a DSSE envelope under a pinned namespace, keyed to the commit the event resolves to, and verifies it against a trust root computed from the author identity's ticket. An identity is itself a rebar ticket of type identity carrying a keys list, and `rebar.add_identity_key` seeds and rotates that keyring with trust-on-first-use for the genesis key and a signed key-operation envelope for later rotations. The roadmap made this feature contingent on the object store detaching authorship from the git store, so the first work is an evaluation, and the implementation follows when the evaluation clears it.

### Requirements

- R1. The plan records whether the S3-backed rebar ticket store detaches rebar authorship from the git store, with the evaluation grounded in the installed rebar authorship behavior, and the finding gates the rest of the work.
- R2. When the evaluation clears implementation, each authoring agent has a rebar identity with a keyring seeded through trust-on-first-use, and a signing key provisioned to the worker that authors changes.
- R3. The plan authorship and the code-change authorship are each signed under the authoring agent's identity through the rebar authorship substrate.
- R4. A merge gate verifies both authorship signatures against the identity trust root at the resolved commit and fails closed when a signature is absent, invalid, or signed by a key outside the identity's trust root.
- R5. The trust root is the identity's keyring, key rotation goes through a signed key-operation that admits a new key before the old key retires, key revocation removes a compromised key, and every keyring change writes an audit record.
- R6. When the evaluation concludes signing is infeasible on the available store, the plan records the residual coupling and the decision and makes no signing change, and in all cases a Jira-only deployment's non-participation is documented.

### Scope Boundaries

- Authored-identity signing is a rebar-backend capability. A Jira-only deployment does not participate, and this plan does not build a Jira authorship substrate.
- This plan does not build a key-management service. It consumes signing key paths provisioned to the worker and manages the keyring through the rebar identity substrate.
- Signing review verdicts is a separate roadmap feature with its own plan and is not delivered here.
- Standing up the S3 and MinIO object store is delivered elsewhere. This plan evaluates its effect on authorship and consumes it, it does not provision it.

### Resolved Decisions

- Resolved in planning. The authorship substrate is `rebar.attest.authorship`, not a bespoke signer, so the platform reuses rebar's in-toto statement, DSSE envelope, pinned namespace, commit resolution, and trust-root computation from the identity ticket.
- Resolved in planning. An authoring agent's identity is a rebar identity ticket with a keyring, and its trust root is resolved through `rebar.attest.authorship.resolve_trust_root`, so the platform does not maintain a parallel identity registry.

### Sources

- Roadmap feature and verification table: [2026-09-02-1801-feat-remaining-work-roadmap-plan.md](docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md).
- Object-store and authorship coupling context: `docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md` (the rebar ticket store on a dedicated git orphan branch synced to a private S3 bucket through `git-remote-s3`), `.agent-context/agentic_plan_v2.md` section 10 (sensitive-data and shared-host concern).
- rebar authorship substrate (installed `nava-rebar` 0.12.0): `rebar.attest.authorship` (`sign_authorship(payload, key_path, principal) -> Envelope`, `sign_event_authorship(event, key_path, principal) -> Envelope`, `verify_authorship_at_commit(envelope, identity_id, event_commit, event_position, *, repo_root, position_resolver) -> Verdict`, `verify_authorship_any_key`, `build_authorship_statement`, `authorship_content_hash`, `resolve_trust_root`, `allowed_signers_from_keys`, `identify_signer`, `keyop_payload`, `keys_valid_at_anchor`), `rebar.add_identity_key(identity_id, public_key, *, signature=None)` and `rebar.revoke_identity_key(...)` (keyring with trust-on-first-use genesis and signed key-operation rotation), `rebar.attest.dsse` (`Envelope`), `rebar.attest.registry` (`Verdict`).
- Merge gate seam: the promotion and terminal path in `services/orchestration/temporal_workflows/draft_pull_request.py` and the promotion policy mediator.
- Authoring stages: `services/orchestration/temporal_workflows/` plan and implementation workflows that produce the plan and the code change.
- Ticket-backend capability model precedent: `services/integration/tickets/backend.py` (an optional capability implemented for rebar and absent on Jira).
- Cross-plan dependency: the [Signed review verdicts plan](2026-09-11-2100-feat-signed-review-verdicts-plan.md) also manages a trust root with rotation, revocation, and audit over the same rebar attest substrate, so this feature shares that plan's audit helper and trust-root-change record shape rather than building a parallel one.

## Planning Contract

### Key Technical Decisions

- KTD1. Gate the implementation on a recorded evaluation. U1 evaluates and records whether authorship detaches from the git store, and the later units proceed only when it clears them. Governs R1, R6. Rationale. The roadmap made the feature contingent, so the honest first step is the evaluation, not the code.
- KTD2. Model each authoring agent as a rebar identity, not a platform identity registry. Seed the keyring through `rebar.add_identity_key` with trust-on-first-use and resolve the trust root through `resolve_trust_root`. Governs R2, R5. Rationale. rebar already carries the identity ticket, keyring, and trust-root computation, so a parallel registry would duplicate and drift.
- KTD3. Sign authorship at the point each artifact is produced, under the producing agent's identity. Sign the plan authorship when the plan is accepted and the code authorship when the change is committed, through `sign_event_authorship` or `sign_authorship`. Governs R3. Rationale. Authorship binds an artifact to its author at production time, so the signature is issued where the artifact is created.
- KTD4. Verify at a merge gate and fail closed. The merge gate verifies both signatures through `verify_authorship_at_commit` against the identity trust root at the resolved commit and blocks on any negative verdict. Governs R4. Rationale. An unverifiable authorship claim at merge is worse than a blocked merge, so doubt blocks.
- KTD5. Treat the keyring as the rotation and revocation substrate. Rotation is a non-genesis `add_identity_key` carrying a signed key-operation envelope, revocation is `revoke_identity_key`, and both write an audit record. Governs R5. Rationale. Keys rotate and leak, and rebar's keyring already models a signed rotation that admits a new key before the old key retires.
- KTD6. Make signing an optional rebar-backend capability. Authored-identity signing activates on the rebar backend and is absent on Jira, mirroring the dependency-capability pattern. Governs R6. Rationale. The substrate is rebar-specific, so a Jira-only deployment cleanly does not participate rather than failing.

### High-Level Design

U1 evaluates the coupling against the installed rebar authorship behavior and records the finding and decision. When it clears implementation, each authoring agent is provisioned a rebar identity whose keyring is seeded through trust-on-first-use and a signing key path on the authoring worker. When a plan is accepted, the plan authorship is signed under the agent identity through the rebar authorship substrate, and when the code change is committed, the code authorship is signed the same way, each producing a DSSE envelope keyed to the resolved commit. A merge gate resolves the author identity's trust root, verifies both envelopes at the resolved commit, and blocks the merge on any absent, invalid, or out-of-trust-root signature. Key rotation and revocation go through the rebar keyring with a signed key-operation and an audit record. On the Jira backend the capability is absent and the merge gate does not require an authorship signature.

### Assumptions

- ssh-keygen is available in the authoring and merge-gate runtimes, which the rebar authorship substrate needs for SSHSIG.
- Each authoring agent's signing key is provisioned to the worker as a file path out of band, the same way other secrets reach the worker.
- The rebar ticket store is the backend when signing is active, since the identity tickets and authorship events live in that store, and the v0.1.0 backend keeps that store isolated on a dedicated orphan branch.

### Sequencing

U1 is the evaluation and gate. U2 provisions identities and keys. U3 signs plan and code authorship. U4 verifies at the merge gate. U5 adds rotation, revocation, and audit. U6 proves the flow end to end and documents it. U2 through U6 proceed only when U1 clears them. U3 depends on U2, U4 depends on U3, U5 depends on U2, U6 depends on U3 through U5.

## Implementation Units

### U1. Evaluate and record the authorship-to-git-store coupling

Goal. Evaluate whether the S3-backed rebar ticket store detaches rebar authorship from the git store, record the finding and the decision, and gate the rest of the work on it.
Requirements. R1, R6.
Files. `docs/` (an evaluation note recording the finding and decision), this plan's status.
Approach. Establish from the installed rebar authorship behavior where authorship state lives, that identities are rebar tickets with keyrings and that authorship events resolve to commits, and how the S3 option relates to that git structure. Record whether the object store detaches authorship or leaves it git-anchored, and decide whether implementation proceeds on the rebar backend given the v0.1.0 store isolation. When the finding is that signing is infeasible on the available store, stop after recording the residual coupling and the decision.
Test scenarios. The evaluation note states where authorship state lives, whether the object store detaches it, and a proceed or stop decision. When the decision is stop, no signing code is added. When the decision is proceed, the note names the rebar backend as the signing-capable backend and Jira as non-participating.
Verification. The recorded evaluation note, plan review.

### U2. Authoring-agent identities and signing keys

Goal. Give each authoring agent a rebar identity with a keyring seeded through trust-on-first-use and a signing key provisioned to the authoring worker.
Requirements. R2.
Files. `services/orchestration/` identity provisioning helper, `scripts/config.py` (`authorship_signing_key_path` and `authorship_identity_id`), `.env.example` and `docs/` (provisioning documentation).
Approach. Create or resolve a rebar identity ticket per authoring agent and seed its keyring with the agent's public key through `rebar.add_identity_key` under trust-on-first-use for the genesis key. Provision the matching private signing key to the authoring worker as a file path the config exposes. Resolve the identity trust root through `resolve_trust_root` to confirm the keyring is in place.
Test scenarios. A new agent identity seeds a genesis key through trust-on-first-use and its trust root resolves to that key. A second genesis attempt on a keyed identity is refused. The worker config exposes the signing-key path and the identity id, and a missing key surfaces a clear error.
Verification. `tests/unit/test_authorship_identity.py` (new), `inv test`.

### U3. Sign plan and code authorship

Goal. Sign the plan authorship when the plan is accepted and the code authorship when the change is committed, under the authoring agent's identity.
Requirements. R3.
Files. `services/orchestration/temporal_workflows/` plan and implementation workflows or their activities (issue the authorship signature at each production point), a signing helper over `rebar.attest.authorship`.
Approach. When the plan is accepted, build the plan authorship event and sign it through `sign_event_authorship` or `sign_authorship` under the agent identity, producing a DSSE envelope keyed to the resolved commit. Do the same for the code change when it is committed. Persist each envelope with the artifact so the merge gate can verify it. Make the signing step a no-op on a backend without the authorship capability.
Test scenarios. An accepted plan produces an authorship envelope under the agent identity. A committed code change produces an authorship envelope under the agent identity. On a non-rebar backend, no authorship envelope is produced and the flow proceeds. A missing signing key raises a clear error rather than silently skipping on the rebar backend.
Verification. `tests/unit/test_authorship_signing.py` (new), `inv test`.

### U4. Merge-gate authorship verification

Goal. Verify the plan and code authorship signatures at a merge gate against the identity trust root and fail closed on any negative verdict.
Requirements. R4.
Files. `services/orchestration/temporal_workflows/draft_pull_request.py` (the promotion or terminal gate), the promotion policy mediator, a verification helper over `verify_authorship_at_commit`.
Approach. At the merge gate, resolve the author identity's trust root and verify both authorship envelopes at the resolved commit through `verify_authorship_at_commit`. Block the merge when either signature is absent, returns a non-certified verdict, or is signed by a key outside the identity's trust root. On a backend without the authorship capability, do not require a signature.
Test scenarios. Two valid signatures verify and the merge proceeds. An absent signature blocks. An invalid or tampered signature blocks. A signature from a key outside the identity trust root blocks. On a non-rebar backend the gate does not require a signature and proceeds.
Verification. `tests/unit/test_authorship_merge_gate.py` (new), `tests/unit/test_draft_pull_request_workflow.py` (extend), `inv test`.

### U5. Rotation, revocation, and audit

Goal. Support key rotation and revocation through the rebar keyring and write an audit record for every keyring change.
Requirements. R5.
Files. `services/orchestration/` keyring management helper, the audit path used by the write-back contract.
Approach. Rotate a key through a non-genesis `rebar.add_identity_key` carrying a signed key-operation envelope over the key-operation payload, so a new key is admitted before the old key retires. Revoke a compromised key through `rebar.revoke_identity_key`. Write an audit record naming the actor, the operation, the identity, and the affected key on every keyring change.
Test scenarios. A rotation admits a new key with a valid signed key-operation and both keys verify during the rollout window. A rotation without a valid signature is refused. A revoked key no longer verifies. Every keyring change writes an audit record with the actor, operation, identity, and key.
Verification. `tests/unit/test_authorship_keyring.py` (new), `inv test`.

### U6. End-to-end proof and documentation

Goal. Prove a signed plan and code change verify at the merge gate and document authored-identity signing as a rebar-backend capability.
Requirements. R3, R4, R6.
Files. `tests/integration/` (an authored-identity signing scenario), `README.md` and `CHANGELOG.md`.
Approach. Drive a run on the rebar backend where an agent signs the plan and code authorship and the merge gate verifies both and admits the merge, then a run where a tampered signature blocks. Document the capability, the identity and key provisioning, the rotation and revocation story, and the Jira non-participation.
Test scenarios. A signed run verifies at the merge gate and merges. A tampered-signature run blocks at the merge gate. A Jira-backend run does not require a signature and is documented as non-participating.
Verification. `tests/integration` authored-identity scenario, `inv ci`, `inv test`.

## Verification Contract

- Lint. `ruff check` clean.
- Types. `basedpyright` clean on the changed source, with no new baseline entries in the changed files.
- Unit and end-to-end. `inv ci` green.
- Full suite. `inv test` green, including `tests/integration`.
- Gate floor. When signing is active, an absent, invalid, or out-of-trust-root authorship signature blocks the merge in every case the plan names.
- Regression floor. On a backend without the authorship capability, the merge path behaves exactly as before and every pre-existing workflow test passes with no assertion change.

## Definition of Done

- Global. R1 through R6 are met. `inv ci` and `inv test` are green. Abandoned or experimental code is removed from the diff.
- U1. The coupling evaluation is recorded with a proceed or stop decision, and the rest of the work is gated on it.
- U2. Each authoring agent has a rebar identity with a trust-on-first-use keyring and a provisioned signing key.
- U3. The plan and code authorship are signed under the agent identity at their production points, and signing is a no-op on a non-capable backend.
- U4. The merge gate verifies both signatures against the identity trust root and fails closed on any negative verdict.
- U5. Key rotation and revocation go through the rebar keyring with a signed key-operation and an audit record.
- U6. A signed run verifies and merges, a tampered run blocks, and the capability and its Jira non-participation are documented.
