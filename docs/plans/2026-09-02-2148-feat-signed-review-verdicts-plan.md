---
title: Signed Review Verdicts - Plan
type: feat
date: 2026-09-02
origin: docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-brainstorm
execution: code
---

# Signed Review Verdicts - Plan

## Goal Capsule

Objective. A plan or code review verdict is trustworthy to a verifier who holds only a public trust root, and the gate refuses to promote a verdict whose signature is absent or invalid.

Means. Add a rebar attest SSHSIG attestor behind the existing `Attestor` protocol, selected by configuration alongside the existing `HmacAttestor`, verified against a supplied `allowed_signers` trust root (KTD1, KTD2, KTD4).

Product authority. Enriches the "Signed review verdicts" feature in the roadmap at [remaining-work roadmap plan](docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md) and the roadmap's v0.2.0 grouping in [ROADMAP.md](ROADMAP.md). Design intent comes from `.agent-context/agentic_plan_v2.md` Phase 6 Option A and Phase 16.

Stop conditions. The Postgres evidence sink stays the store of record. The verify path is store-independent: the attestor loads the trust root from its configured path and passes it to each rebar verify call. No git ticket store is required.

## Product Contract

Product Contract preservation. Sourced from the "Signed review verdicts" roadmap feature; requirements restate its subtasks with no scope change.

### Summary

Route the plan and code review attestations through rebar's `rebar.attest` SSHSIG substrate in place of the hand-rolled `HmacAttestor`. A verifier needs no shared secret, only the public `allowed_signers` trust root. The gate fails closed when a signature is absent or does not verify.

### Problem Frame

The review gate signs each verdict with `HmacAttestor`, a symmetric HMAC keyed by `review_attestation_secret`. Any holder of that secret can forge a verdict, and a verifier must hold the same secret, so the attestation proves little to an external auditor. rebar ships an asymmetric SSHSIG substrate that verifies against a public trust root, which is the Phase 6 Option A design.

### Requirements

- R1. Plan and code review verdicts carry a rebar attest SSHSIG signature over the canonical verdict payload.
- R2. The signature verifies against a supplied public `allowed_signers` trust root, with no shared secret on the verify path.
- R3. The gate fails closed when a verdict signature is absent, malformed, or does not verify.
- R4. The Postgres evidence sink stays the store of record; the attestation fields it persists carry the SSHSIG algorithm, key id, and signature.
- R5. The signing key is delivered as a `0600` file, an `allowed_signers` trust root ships alongside, and the review images carry `ssh-keygen` 8.9 or newer.
- R6. The `HmacAttestor` stays available only in an explicit local-development mode. A deployed review worker or CI gate fails closed unless `review_attestor=sshsig` with both the signing-key and trust-root paths present, so a deployed environment cannot silently fall back to HMAC.

### Scope Boundaries

- No change to the verdict payload shape or the multi-pass kernel; only the attestor changes.
- Authored-identity signing (per-agent authorship) is a separate feature and out of scope here.
- No git ticket store dependency; this is the store-independent attest path.

### Outstanding Questions

- Deferred to planning. Resolved here: the `Attestation.signature` stores the DSSE envelope string, and verification goes through `registry.verify(kind, envelope, trust_root)`, which selects the SSHSIG scheme by policy kind. The raw-blob path is not used.

### Sources

- Swap seam: `services/review/attestation.py` (`Attestor` protocol, `HmacAttestor`, `content_hash`, `canonical_bytes`, `Attestation`), `services/review/gate.py` (`ReviewGate.__init__` attestor injection at the default line, `review` sign-then-verify, evidence metadata).
- Attestation type: `services/review/types.py` (`Attestation` with `algorithm`, `payload_version`, `content_hash`, `signature`, `key_id`, `issued_at`).
- Downstream hash flow: `services/orchestration/temporal_activities/review.py` (`attestation_hash=result.attestation.content_hash`), `services/orchestration/temporal_activities/gates.py`, `services/orchestration/temporal_workflows/draft_pull_request.py`.
- rebar substrate (installed `nava-rebar` 0.12.0): `rebar/attest/sshsig.py` (`sign(pae_bytes, key_path, namespace)`, `SshsigScheme.verify(pae_bytes, signatures, namespace, trust_root)`, `ensure_available`, `SshKeygenUnavailable`), `rebar/attest/dsse.py` (`pae`, `Envelope`, `Signature`, `encode`), `rebar/attest/registry.py` (`verify(kind, envelope, trust_root)`, `Verdict`).
- Config: `scripts/config.py` (`review_attestation_secret`).

## Planning Contract

### Key Technical Decisions

- KTD1. Add a `RebarSshsigAttestor` that conforms to the existing `Attestor` protocol (`sign`/`verify`) so `ReviewGate` needs no structural change. Governs R1, R3. Rationale: the gate already injects an `Attestor` and fails closed when `verify` returns false, so a new conforming attestor is the whole seam.
- KTD2. Sign over the same `content_hash(verdict_payload)` the gate already computes, wrapped in a DSSE pre-auth encoding, store the result as a DSSE envelope string, and verify through `registry.verify(kind, envelope, trust_root)` against the supplied `allowed_signers` trust root. Governs R1, R2. Rationale: reusing the canonical payload keeps the signed subject identical to today; `registry.verify` selects the SSHSIG scheme by policy kind and keeps the verify path store-independent.
- KTD3. Populate `Attestation` with `algorithm="SSHSIG"`, `key_id` as the `allowed_signers` principal used as the DSSE `Signature.keyid`, never a bare fingerprint (which would fail `ssh-keygen -Y verify -I`), and `signature` as the encoded DSSE envelope, leaving the type unchanged. Governs R4. Rationale: the existing fields carry an SSHSIG attestation without a schema change, and the principal is what the verifier matches against the trust root.
- KTD4. Select the attestor by config. A deployed or CI environment fails closed unless `review_attestor=sshsig` with the key and trust-root paths set; `HmacAttestor` is available only in an explicit local-development mode. Governs R6. Rationale: a silent HMAC fallback in a deployed environment would defeat the asymmetric-verification goal.
- KTD5. Require `ssh-keygen` 8.9 or newer in the review images and preflight it through rebar's `ensure_available`, failing closed when absent. Governs R3, R5. Rationale: rebar shells to `ssh-keygen -Y sign`/`-Y verify`.

### High-Level Design

One new attestor, one config selector, one image and key-provisioning change.

- Attestor. `RebarSshsigAttestor` implements `sign(payload)` by computing `content_hash`, building the DSSE PAE, calling `rebar.attest.sshsig.sign` with the `0600` key path and the review namespace, and returning an `Attestation`. `verify(attestation, payload)` recomputes the hash and calls the SSHSIG scheme verify against the trust root, returning false on any mismatch or on `SshKeygenUnavailable`.
- Selection. `ReviewGate` keeps its `attestor` parameter. A factory reads the config flag and the key and trust-root paths, returns `RebarSshsigAttestor` when configured, else `HmacAttestor`.
- Fail-closed. The gate already raises `ReviewAttestationError` when `verify` is false, so an absent or invalid signature stops promotion without new control flow.
- Provisioning. The review images carry `ssh-keygen` 8.9+, the signing key is mounted `0600`, and the `allowed_signers` trust root ships alongside.

### Assumptions

- rebar 0.12.0's `rebar/attest/sshsig.py` and `dsse.py` work as installed, verified present.
- The review images can carry `ssh-keygen` 8.9+ and a `0600` key mount.
- The canonical `verdict_payload` is the correct signed subject, unchanged from today.

### Sequencing

U1 (attestor) and U2 (config selection) are the core; U2 depends on U1. U3 (images, key, trust root) is independent infrastructure. U4 (verification) depends on U1 through U3.

## Implementation Units

### U1. RebarSshsigAttestor

Goal. Add an SSHSIG attestor conforming to the `Attestor` protocol.
Requirements. R1, R2, R3, R4.
Dependencies. None.
Files. `services/review/attestation.py` (add `RebarSshsigAttestor`), `services/review/types.py` (unchanged unless a field is missing).
Approach. Define exact constants for the review-verdict DSSE payload type and the SSHSIG namespace, and use them in sign, verify, and tests. `sign` computes `content_hash(payload)`, builds the DSSE PAE over it with the pinned payload type, calls `rebar.attest.sshsig.sign(pae, key_path, namespace)`, and returns an `Attestation` with `algorithm="SSHSIG"`, `key_id` set to the `allowed_signers` principal, and `signature` set to the encoded DSSE envelope (KTD2, KTD3). `verify` calls `sshsig.ensure_available` first, recomputes the hash and PAE, and verifies through `registry.verify(kind, envelope, trust_root)` loaded from the configured trust-root path, returning false on mismatch. Map `SshKeygenUnavailable`, `FileNotFoundError`, `OSError`, `subprocess.CalledProcessError`, malformed DSSE or SSHSIG decode errors, and any non-verified `Verdict` to a fail-closed false, so no exception escapes the attestor.
Test scenarios. A signed payload verifies against its trust root. A tampered payload fails verify. A signature from a key outside the trust root fails verify. A missing signature fails verify. An existing SSHSIG attestation verifies using only the `allowed_signers` trust root, with no private signing key path and no HMAC secret available to the verifier. `ssh-keygen` absent, a `CalledProcessError`, and a malformed envelope each yield a false verify, not an exception escaping the attestor.
Verification. `tests/unit/test_review_attestation_sshsig.py` (new, over a generated test key and allowed_signers), `inv test`.

### U2. Config-selected attestor

Goal. Select the attestor by configuration, failing closed in deployed and CI environments unless SSHSIG is fully configured.
Requirements. R6.
Dependencies. U1.
Files. `scripts/config.py` (add `review_attestor`, an explicit local-development mode flag, and key and trust-root path settings), `services/review/gate.py` (a factory resolves the attestor from config), `.env.example`.
Approach. Add a `review_attestor` setting. In a deployed or CI environment, `ReviewGate` requires `sshsig` with the key and trust-root paths and fails closed with a clear error otherwise. `HmacAttestor` is selected only under an explicit local-development mode flag (KTD4). Keep the explicit `attestor` constructor argument for tests.
Test scenarios. The local-development mode yields `HmacAttestor`. A deployed or CI environment with `review_attestor` unset or `hmac` raises a clear error rather than falling back. Setting `sshsig` with key and trust-root paths yields `RebarSshsigAttestor`. Setting `sshsig` without the paths fails closed.
Verification. `tests/unit/test_review_gate.py` (extend), `inv test`.

### U3. Signing key, trust root, and image dependency

Goal. Provision the signing key, the trust root, and `ssh-keygen` 8.9+ in the review images, and prove rotation.
Requirements. R5.
Dependencies. None.
Files. `docker/` review-runtime image (install `ssh-keygen` 8.9+), `compose.yaml`/`compose.override.yaml` (mount the `0600` key and the `allowed_signers` trust root into the review worker), `.env.example`, deployment docs under `docs/`.
Approach. Ensure `ssh-keygen` 8.9+ is present and preflighted through rebar's `ensure_available`. Deliver the signing key as a `0600` file and ship the `allowed_signers` trust root alongside. Document and test rotation of the signing key and the trust root.
Test scenarios. A preflight reports `ssh-keygen` present and at 8.9+. A missing or old `ssh-keygen` fails the preflight closed. The key file is mounted `0600` and the trust root is readable. A rotation drill over generated keys shows the updated trust root accepts the new signer and rejects a retired signer.
Verification. `tests/unit/test_review_signing_preflight.py` (new, including the rotation drill), manual `inv local-up` note, `inv test`.

### U4. Signed-verdict verification

Goal. Prove signed verdicts flow through the gate and evidence and fail closed on tampering.
Requirements. R1, R3, R4.
Dependencies. U1, U2, U3.
Files. `tests/unit/test_review_gate.py` (extend), `tests/integration/` signed-verdict scenario (new).
Approach. Run the gate with the SSHSIG attestor and assert the persisted evidence record carries the SSHSIG algorithm, key id, and signature, and that `ReviewOutcome.attestation_hash` matches. Assert a mutated payload or a stripped signature raises `ReviewAttestationError` and no promotion proceeds.
Test scenarios. A plan and a code verdict carry a verifying SSHSIG signature in evidence. A tampered verdict fails closed. A stripped signature fails closed. The evidence metadata records algorithm, key id, and signature.
Verification. `tests/unit/test_review_gate.py`, the new integration scenario, `inv ci`.

## Verification Contract

- Lint. `ruff check` clean. Types. `basedpyright` clean on `services/review`.
- Unit and end-to-end. `inv ci` green.
- Full suite. `inv test` green, including `tests/integration`.
- Regression floor. In the explicit local-development mode, `tests/unit/test_review_gate.py` and `tests/unit/test_review_attestation.py` pass unchanged. A deployed or CI environment fails closed unless SSHSIG is fully configured.

## Definition of Done

- Global. R1 through R6 are met. `inv ci` and `inv test` are green. In the local-development mode, review attestation behaves as today; a deployed or CI environment fails closed unless SSHSIG is fully configured. Abandoned code is removed from the diff.
- U1. `RebarSshsigAttestor` conforms to the protocol, verifies against a trust root using only public material, and maps every ssh-keygen and decode failure to a fail-closed verify.
- U2. The attestor is config-selected, fails closed in deployed and CI environments unless SSHSIG is fully configured, and offers HMAC only under the explicit local-development mode.
- U3. The review images carry `ssh-keygen` 8.9+, the `0600` key, and the trust root, with a passing rotation drill that accepts a new signer and rejects a retired one.
- U4. Signed plan and code verdicts verify in evidence and fail closed on tampering or an absent signature.
