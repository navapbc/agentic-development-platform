---
title: Signed Review Verdicts - Plan
type: feat
date: 2026-09-11
origin: docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-brainstorm
execution: code
---

# Signed Review Verdicts - Plan

## Goal Capsule

Objective. Every review verdict carries a rebar attest signature over the SSHSIG scheme wrapped in a DSSE envelope, the review gate verifies that signature against a public trust root before it accepts a verdict, and the gate fails closed when the signature is absent or invalid.

Means. Replace the platform `HmacAttestor` behind the existing `Attestor` protocol with an attestor that signs the canonical verdict payload through `rebar.attest.sshsig` inside a `rebar.attest.dsse.Envelope`, and verifies it against a configured trust root of allowed signer keys. The gate keeps its sign-then-verify shape and raises `ReviewAttestationError` on any failure, so the review activity blocks rather than admits an unattested verdict.

Product authority. This plan enriches one feature from the roadmap at [2026-09-02-1801-feat-remaining-work-roadmap-plan.md](docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md), the "Signed review verdicts" entry. It does not widen that feature's scope.

Stop conditions. The `Attestor` protocol and the `ReviewGate` sign-then-verify contract are unchanged in shape. The HMAC path stays available for local development that configures no signing key. Trust-root distribution is a configuration input, not a key-management service this plan builds.

## Product Contract

Product Contract preservation. Sourced from the roadmap feature "Signed review verdicts". The requirements below restate that feature's subtasks as R-IDs with no scope change.

### Summary

The review gate today signs each verdict with a symmetric HMAC over the canonical payload, using one shared secret that both signs and verifies. A holder of the secret can forge a verdict, and the evidence record carries no proof of which key signed beyond a truncated fingerprint. This plan moves every review verdict onto asymmetric SSHSIG signatures produced through rebar's attest substrate and carried in DSSE envelopes. The gate verifies each signature against a public trust root of allowed signer keys before it accepts the verdict, and fails closed when the signature is missing, malformed, signed by a key outside the trust root, or does not match the payload. The verifier holds only public keys, so it cannot forge a verdict. The symmetric HMAC path remains available for local development when `REVIEW_SIGNING_ENABLED` is set to false.

### Problem Frame

`services/review/gate.py` builds an `HmacAttestor(get_settings().review_attestation_secret)` and signs `verdict_payload(review, raw)` with HMAC-SHA256, then verifies with the same secret and persists the signature and key fingerprint into the evidence record. This gives integrity under a shared secret but no authenticity a third party can check, because verification needs the same secret that signs. rebar 0.12.0 ships an attest substrate the platform does not yet use for review verdicts. `rebar.attest.sshsig` signs a pre-authentication-encoded byte string with an ssh private key under a namespace, `rebar.attest.dsse` wraps the payload and signature in an envelope with a canonical encoding, and `rebar.attest.registry` carries the scheme and a verdict type. This feature adopts that substrate for every review stage.

### Requirements

- R1. Every review verdict is signed with the rebar SSHSIG scheme over the canonical verdict payload, wrapped in a DSSE envelope, replacing the HMAC signature for every review stage when signing is configured.
- R2. A public trust root of allowed signer keys is supplied by configuration and loaded at gate construction, and the gate verifies every signature against it before accepting a verdict.
- R3. The gate fails closed with `ReviewAttestationError` when a verdict signature is absent, malformed, does not match the payload, or is produced by a key outside the trust root.
- R4. The `Attestor` protocol and the `ReviewGate` sign-then-verify flow keep their shape, so the change is a substitution behind the protocol rather than a rewrite of the gate.
- R5. The evidence record carries the signing scheme name, the signer key identifier, the envelope, and enough detail to re-verify the verdict offline against the trust root.
- R6. The trust root is a versioned bundle carrying key identifiers, supports revocation of a compromised key and a rotation rollout that admits a new key before the old key retires, and every trust-root change writes an audit record naming the actor, the change, and the affected key identifiers.
- R7. Signing is on by default. When signing is enabled but the key or trust root is not configured, a signed stage fails closed rather than downgrading to HMAC. Setting `REVIEW_SIGNING_ENABLED` to false selects the HMAC path for local development, and the selection is explicit in configuration rather than silent.

### Scope Boundaries

- All four review stages sign when signing is configured, namely plan review, code review, security review, and acceptance verification.
- This plan does not build a key-management service or a certificate authority. It consumes a trust-root bundle and a signing key path supplied by configuration.
- Signing the manifest of verified completion steps through `rebar.sign_manifest` is a separate concern from signing a review verdict and is out of scope here.
- Authored-identity signing, where each authoring agent signs its own plan and code change, is a separate roadmap feature with its own plan and is not delivered here.

### Resolved Decisions

- Resolved in planning. The signature substrate is `rebar.attest.sshsig` carried in a `rebar.attest.dsse.Envelope`, not a raw ssh-keygen invocation, so the platform reuses rebar's pre-authentication encoding, envelope canonicalization, and verdict registry rather than reimplementing them.
- Resolved in planning. The canonical signed bytes are the DSSE pre-authentication encoding of the existing `verdict_payload` output, so the payload the platform already builds is what gets signed, and a verifier recomputes the same bytes from the stored payload.

### Sources

- Roadmap feature and verification table: [2026-09-02-1801-feat-remaining-work-roadmap-plan.md](docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md).
- Substitution seam: `services/review/gate.py` (`ReviewGate.__init__`, `ReviewGate.review`, `verdict_payload`), `services/review/attestation.py` (`Attestor` protocol, `HmacAttestor`, `canonical_bytes`, `content_hash`, `ReviewAttestationError`), `services/review/types.py` (`Attestation`).
- Evidence record fields: `services/review/evidence.py` (`EvidenceRecord`, `EvidenceSink`).
- rebar attest substrate (installed `nava-rebar` 0.12.0): `rebar.attest.sshsig` (`sign(pae_bytes, key_path, namespace) -> bytes`, `ensure_available`, `SshsigScheme`, `SCHEME_NAME`, `register_sshsig_scheme`), `rebar.attest.dsse` (`Envelope`, `Signature`, `encode`, `decode`, `pae`), `rebar.attest.registry` (scheme and policy registry, `Verdict`).
- Config: `scripts/config.py` (the existing `review_attestation_secret`, plus new `review_signing_key_path`, `review_trust_root_path`, and `review_signing_enabled` as the path-selection flag).
- Audit sink for trust-root changes: `services/orchestration` audit path used by the ticket write-back contract.
- Cross-plan dependency: the [Native Bedrock review path plan](2026-09-02-2042-feat-native-bedrock-review-path-plan.md) stands up a dedicated `review-worker`. The signing key from this plan mounts on that worker, so this feature sequences after it. The [Authored-identity signing plan](2026-09-11-2120-feat-authored-identity-signing-plan.md) also manages a trust root with rotation, revocation, and audit, so both features share one audit helper and one trust-root-change record shape rather than diverging.

## Planning Contract

### Key Technical Decisions

- KTD1. Substitute behind the `Attestor` protocol rather than change the gate. Add an `SshsigAttestor` implementing `sign` and `verify` and select it in `ReviewGate.__init__` from configuration, leaving `HmacAttestor` as the fallback. Governs R1, R4, R7. Rationale. The gate already depends on the protocol and injects the attestor, so the smallest correct change is a second implementation and a selector.
- KTD2. Sign the DSSE pre-authentication encoding of the existing canonical payload. The attestor builds the envelope over `canonical_bytes(payload)` and signs `dsse.pae(...)` through `sshsig.sign`. Governs R1, R5. Rationale. Reusing the payload the gate already computes keeps the signed content identical to what the evidence record stores, so offline re-verification recomputes the same bytes.
- KTD3. Verify against a loaded trust root, never against the signing key. The attestor loads the allowed-signer trust root at construction and verifies the envelope signature against it, so a verifier holds only public keys. Governs R2, R3. Rationale. Asymmetric verification is the property the HMAC path lacks, and loading at construction fails a misconfigured trust root fast.
- KTD4. Fail closed on every negative outcome. Absent envelope, malformed envelope, payload mismatch, and out-of-trust-root signer all raise `ReviewAttestationError`, which the gate already surfaces. Governs R3. Rationale. A review gate that admits an unverifiable verdict is worse than one that blocks, so the default on doubt is block.
- KTD5. Treat the trust root as a versioned bundle with revocation and rotation. The bundle carries key identifiers and a validity window per key, verification rejects a revoked key, and a rollout window admits a new key before the old key retires. Governs R6. Rationale. Keys rotate and leak, so verification has to distinguish a currently-trusted key from a retired or revoked one without a flag day.

### High-Level Design

An `SshsigAttestor` implements the `Attestor` protocol. Its `sign` builds a DSSE envelope over the canonical verdict payload and signs the envelope's pre-authentication encoding through `rebar.attest.sshsig.sign` with the configured signing key and the review namespace, returning an `Attestation` whose fields carry the scheme name, the signer key identifier, and the serialized envelope. Its `verify` decodes the envelope, recomputes the canonical payload bytes, and verifies the signature against the loaded trust root through the rebar registry, returning false on any mismatch or out-of-trust-root signer. `ReviewGate.__init__` selects the attestor from configuration, defaulting to `HmacAttestor` when no signing key is set. The evidence record gains the scheme, the signer key identifier, and the envelope so a verdict re-verifies offline. A trust-root loader reads the versioned bundle, applies revocation and validity windows, and writes an audit record on every change.

### Assumptions

- ssh-keygen is available in the review runtime, which `rebar.attest.sshsig.ensure_available` checks, and the plan surfaces a clear error when it is not.
- The signing key is provisioned to the review worker out of band as a file path, the same way other secrets reach the worker, and this plan consumes the path rather than issuing the key.
- The DSSE envelope and the SSHSIG scheme in the installed rebar 0.12.0 are stable enough to serialize into evidence and re-verify later.

### Sequencing

U1 adds the attestor and its unit contract. U2 adds trust-root loading with revocation and rotation. U3 wires the gate selector and the evidence fields. U4 handles the trust-root audit and configuration. U5 proves fail-closed behavior end to end. U2 depends on U1. U3 depends on U1 and U2. U4 depends on U2. U5 depends on U1 through U4.

## Implementation Units

### U1. SSHSIG-over-DSSE attestor behind the protocol

Goal. Add an `SshsigAttestor` that signs the canonical verdict payload through the rebar SSHSIG scheme in a DSSE envelope and verifies it, implementing the existing `Attestor` protocol.
Requirements. R1, R4, R5.
Files. `services/review/attestation.py` (add `SshsigAttestor`, a namespace constant, and the scheme name), `services/review/types.py` (extend `Attestation` with the envelope and scheme fields if the existing fields cannot carry them).
Approach. Implement `sign` to build a `dsse.Envelope` over `canonical_bytes(payload)`, sign its pre-authentication encoding through `sshsig.sign(pae_bytes, key_path, namespace)`, and return an `Attestation` carrying the scheme name, the signer key identifier, the content hash, and the serialized envelope. Implement `verify` to decode the envelope, recompute the canonical bytes from the passed payload, and verify against a supplied trust root through the rebar registry, returning false on any mismatch. Call `sshsig.ensure_available` at construction and raise a clear error when ssh-keygen is missing.
Test scenarios. A signed payload verifies against a trust root that contains the signer key. A tampered payload fails verification. A signature from a key outside the trust root fails verification. A malformed or absent envelope fails verification rather than raising an unexpected error. The attestor reports a clear error when ssh-keygen is unavailable.
Verification. `tests/unit/test_review_attestation.py` (extend or add), `inv test`.

### U2. Trust-root loading with revocation and rotation

Goal. Load a versioned trust-root bundle of allowed signer keys with per-key identifiers and validity windows, reject a revoked key, and admit a rotation window where a new key is trusted before the old key retires.
Requirements. R2, R6.
Files. `services/review/trust_root.py` (new, the loader and the allowed-signer resolution), `services/review/attestation.py` (consume the loaded trust root in `verify`).
Approach. Parse the bundle into a set of allowed signer keys, each with a key identifier and a validity window, and expose a resolver the attestor calls during verification. Reject a signature whose key identifier is revoked or outside its validity window. During a rotation window, treat both the new and the retiring key as valid so a rollout does not require a flag day. Build the allowed-signer material the rebar registry expects through `rebar.attest.authorship.allowed_signers_from_keys` or the equivalent registry input.
Test scenarios. A key inside its validity window verifies. A revoked key is rejected even when otherwise well formed. During a rotation window both keys verify, and after the retiring key's window closes only the new key verifies. A malformed bundle fails loading with a clear error rather than admitting all keys.
Verification. `tests/unit/test_trust_root.py` (new), `inv test`.

### U3. Gate selector and evidence fields

Goal. Select the signed attestor in `ReviewGate` from configuration for every review stage, fail closed when signing is enabled without a key or trust root, and record the scheme, signer key identifier, and envelope in the evidence record.
Requirements. R1, R3, R4, R5, R7.
Files. `services/review/gate.py` (`__init__` selects the attestor, `review` records the new evidence fields), `scripts/config.py` (`review_signing_key_path`, `review_trust_root_path`, and the `review_signing_enabled` selection flag).
Approach. In `ReviewGate.__init__`, when a signing key and trust root are configured, build the `SshsigAttestor` with the loaded trust root, otherwise build the `HmacAttestor` as today. Keep the sign-then-verify block, so a signature that does not verify raises `ReviewAttestationError` and blocks. Extend the evidence metadata with the scheme name, the signer key identifier, and the serialized envelope alongside the existing fields.
Test scenarios. With signing configured, a plan or code verdict is signed and verified through the SSHSIG path and the evidence record carries the scheme, key identifier, and envelope. With no signing key configured, the gate uses the HMAC path and pre-existing tests pass unchanged. A verdict whose signature does not verify raises `ReviewAttestationError` and no evidence record is persisted for an accepted verdict.
Verification. `tests/unit/test_review_gate.py` (extend), `inv test`.

### U4. Trust-root audit and configuration

Goal. Write an audit record for every trust-root change and document the bundle format, the signing-key provisioning, and the path selection.
Requirements. R6, R7.
Files. `services/review/trust_root.py` (emit the audit record on load-time change detection or on an explicit management entry point), `.env.example` and `docs/` (document the bundle, the key path, and the selection flag), a trust-root bundle example under `config/`.
Approach. On a trust-root change, write an audit record naming the actor, the change, and the affected key identifiers through the platform audit path the write-back contract already uses. Document the bundle schema, revocation and rotation fields, the signing-key path, and the explicit flag that selects the signed path.
Test scenarios. A trust-root change writes an audit record with the actor, the change, and the key identifiers. The documented example bundle loads cleanly. The selection flag chooses the signed path when set and the HMAC path when unset.
Verification. `tests/unit/test_trust_root.py` (extend), `inv test`.

### U5. Fail-closed verification end to end

Goal. Prove the gate blocks on an absent, tampered, or untrusted signature and admits a valid one, through the review activity path.
Requirements. R3.
Files. `tests/unit/test_review_activities.py` (extend) or `tests/integration/` (a signed-path review scenario).
Approach. Drive a review gate on the signed path with a valid signature and assert the verdict is accepted and recorded. Then drive it with a tampered payload, an absent envelope, and a signature from an out-of-trust-root key, and assert each raises `ReviewAttestationError` and the run follows the block path rather than admitting the verdict.
Test scenarios. A valid signed verdict is accepted and its evidence record re-verifies against the trust root. A tampered payload blocks. An absent envelope blocks. An out-of-trust-root signer blocks. A revoked key blocks.
Verification. `tests/unit/test_review_activities.py`, the signed-path scenario, `inv ci`.

## Verification Contract

- Lint. `ruff check` clean.
- Types. `basedpyright` clean on `services/review` and `scripts/config.py`, with no new baseline entries in the changed source files.
- Unit and end-to-end. `inv ci` green.
- Full suite. `inv test` green, including `tests/integration`.
- Regression floor. With no signing key configured, every pre-existing test in `tests/unit/test_review_attestation.py`, `tests/unit/test_review_gate.py`, and `tests/unit/test_review_activities.py` passes with no assertion change.
- Fail-closed floor. A tampered, absent, out-of-trust-root, or revoked signature blocks the verdict in every case the plan names.

## Definition of Done

- Global. R1 through R7 are met. `inv ci` and `inv test` are green. Signing is on by default, and with `REVIEW_SIGNING_ENABLED` set to false the review path uses the HMAC attestor. Abandoned or experimental code is removed from the diff.
- U1. Every review verdict signs and verifies through the SSHSIG-over-DSSE attestor behind the `Attestor` protocol.
- U2. The trust root loads with per-key identifiers and validity windows, rejects a revoked key, and admits a rotation window.
- U3. The gate selects the signed attestor from configuration for every stage, fails closed when signing is enabled without a key or trust root, records the scheme, key identifier, and envelope, and raises on a verification failure.
- U4. Every trust-root change writes an audit record, and the bundle, key path, and selection flag are documented.
- U5. A tampered, absent, untrusted, or revoked signature blocks the verdict, and a valid signature is accepted and re-verifies offline.
