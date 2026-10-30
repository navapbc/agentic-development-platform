---
title: S3-backed WORM Evidence and Audit Store - Plan
type: feat
date: 2026-09-14
origin: docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-brainstorm
execution: code
---

# S3-backed WORM Evidence and Audit Store - Plan

## Goal Capsule

Objective. Evidence and audit objects persist to the S3 and MinIO object store under a write-once-read-many retention model, so a record cannot be altered or deleted within its retention window, and the local MinIO path exercises the same model that AWS Object Lock enforces in a deployed environment.

Means. Add an object-store evidence and audit sink beside the Postgres sink, write each record content-addressed under an Object Lock retention, exercise the retention with MinIO locally, map it to AWS S3 Object Lock, and enforce TLS, KMS-managed encryption, a least-privilege bucket policy, retention governance, and access logging on the bucket.

Product authority. This plan enriches one feature from the roadmap at [2026-09-02-1801-feat-remaining-work-roadmap-plan.md](2026-09-02-1801-feat-remaining-work-roadmap-plan.md), the "S3-backed WORM evidence and audit store" entry. It does not widen that feature's scope.

Stop conditions. Evidence and audit objects persist to the object store with a retention that a delete or overwrite cannot bypass within the window, exercised against MinIO locally, with the AWS Object Lock mapping committed. The Postgres record remains as the index.

## Product Contract

Product Contract preservation. Sourced from the roadmap feature "S3-backed WORM evidence and audit store". The requirements below restate that feature's subtasks as R-IDs with no scope change.

### Summary

Evidence persists only to Postgres today. `PostgresEvidenceSink` in `services/review/evidence.py` writes the `evidence_artifacts` row, the `location` field is unused, and there is no object-store sink. v0.1.0 stood up the S3 and MinIO object store for the rebar ticket store, so the store exists but no evidence or audit object lands in it. This plan adds an object-store sink that writes each evidence and audit object content-addressed under a WORM retention, exercises the retention against MinIO locally, and maps it to AWS S3 Object Lock, with the bucket hardened for encryption, least-privilege access, retention governance, and access logging.

### Problem Frame

A compliance evidence record must be tamper-evident and immutable for its retention period. A Postgres row can be updated or deleted by anyone with write access to the database, so Postgres alone cannot make that guarantee. An object store with Object Lock in compliance mode makes a stored object undeletable and unoverwritable until its retention expires. The platform already runs MinIO locally and provisions S3 in terraform, so the sink writes to MinIO for local development and to S3 in a deployed environment, and the Postgres row becomes the queryable index that points at the object.

### Requirements

- R1. An object-store evidence and audit sink writes each evidence object and each audit object to the S3 and MinIO object store, content-addressed by the record's content hash.
- R2. Each written object carries a retention that a delete or an overwrite cannot bypass within the retention window, exercised against MinIO Object Lock locally.
- R3. The AWS S3 Object Lock mapping is committed so a deployed bucket enforces the same retention model.
- R4. The bucket enforces TLS in transit, KMS-managed encryption at rest, a least-privilege bucket policy, Object Lock retention governance, and access logging.
- R5. The Postgres evidence row records the object location so a reader resolves the object from the index.

### Scope Boundaries

- This plan persists evidence and audit objects to the object store. It does not change which artifacts are produced. Routing every artifact type through the sink is a separate roadmap feature with its own plan.
- Setting the retention period value is a separate roadmap feature. This plan consumes a retention value and enforces it in the store.
- This plan does not build a new object store. It writes to the S3 and MinIO store stood up in v0.1.0.

### Resolved Decisions

- Resolved in planning. The object store is the tamper-evident tier and Postgres remains the index, so a reader queries Postgres and resolves the object by its recorded location.
- Resolved in planning. WORM is enforced by Object Lock in compliance mode, exercised locally against MinIO, so the local path proves the same guarantee the AWS mapping enforces.

### Sources

- Roadmap feature and verification table: [2026-09-02-1801-feat-remaining-work-roadmap-plan.md](2026-09-02-1801-feat-remaining-work-roadmap-plan.md).
- Evidence sink and record: `services/review/evidence.py`, `database/models/schema.py` `evidence_artifacts`.
- Object store stood up in v0.1.0: MinIO in `compose.yaml`, and the S3 provisioning in `infra/terraform`.
- Bucket hardening precedent: the hardened exec-log bucket in `infra/terraform/bedrock-jumphost-fargate`.

## Planning Contract

### Key Technical Decisions

- KTD1. Add an object-store sink beside the Postgres sink rather than replacing it. Governs R1, R5. Rationale. Postgres stays the queryable index and the object store is the immutable tier, so each does what it is good at.
- KTD2. Content-address each object by its content hash. Governs R1. Rationale. The evidence record already carries a content hash, so the object key is the hash and a duplicate write is idempotent.
- KTD3. Enforce WORM with Object Lock in compliance mode and exercise it against MinIO locally. Governs R2, R3. Rationale. MinIO supports the same Object Lock API as S3, so the local test proves the deployed guarantee.
- KTD4. Harden the bucket in terraform, not in application code. Governs R4. Rationale. Encryption, the bucket policy, retention governance, and access logging are infrastructure controls, so they live in the module beside the other hardened buckets.
- KTD5. Record the object location on the Postgres row. Governs R5. Rationale. The `location` column exists and is unused, so the sink populates it with the object reference.

### High-Level Design

An object-store sink implements the `EvidenceSink` protocol and a matching audit sink. On persist, it writes the record body to the object store under a key derived from the content hash, with an Object Lock retention set from the record's retention value, then writes or updates the Postgres row with the object location. A local test writes an object against MinIO with a retention and asserts that a delete and an overwrite within the window are refused. A terraform change provisions the deployed bucket with Object Lock, KMS encryption, a TLS-only and least-privilege policy, retention governance, and access logging, mirroring the hardened bucket already in the jump-host module.

### Assumptions

- MinIO in the local stack has Object Lock enabled on the target bucket, or the plan enables it.
- The retention value reaches the sink from the evidence record, which the retention-configuration feature sets.

### Sequencing

U1 adds the object-store sink and content-addressed write. U2 adds the WORM retention and the local MinIO exercise. U3 commits the AWS Object Lock and bucket hardening in terraform. U4 wires the object location onto the Postgres index. U2 depends on U1, U4 depends on U1.

## Implementation Units

### U1. Object-store evidence and audit sink

Goal. Write each evidence and audit object to the S3 and MinIO object store, content-addressed by the record's content hash.
Requirements. R1.
Files. `services/review/evidence.py` (an object-store sink beside `PostgresEvidenceSink`), an object-store client helper, `scripts/config.py` (object-store endpoint, bucket, and credential settings).
Approach. Implement a sink that serializes the record body and puts it under a key derived from the content hash, using the object-store client against the configured endpoint. Make a duplicate put idempotent on the content-addressed key. Add a matching sink for audit objects.
Test scenarios. A record writes an object at the content-addressed key. A second write of the same content is idempotent. A missing endpoint surfaces a clear error.
Verification. `tests/integration/test_object_store_evidence.py` (new), `inv test`.

### U2. WORM retention exercised against MinIO

Goal. Write each object under an Object Lock retention that a delete or overwrite cannot bypass within the window, proven against MinIO locally.
Requirements. R2.
Files. the object-store sink, `tests/integration/test_object_store_evidence.py`.
Approach. Set an Object Lock retention on each put from the record's retention value. Exercise the guarantee against MinIO by attempting a delete and an overwrite within the window and asserting both are refused, and by asserting a read still returns the original bytes.
Test scenarios. A stored object refuses deletion within the window. A stored object refuses overwrite within the window. A read returns the original bytes.
Verification. `tests/integration/test_object_store_evidence.py`, `inv test`.

### U3. AWS Object Lock and bucket hardening

Goal. Provision the deployed evidence bucket with Object Lock and the required controls.
Requirements. R3, R4.
Files. `infra/terraform/` (an evidence-store module or an addition to the object-store terraform), its README.
Approach. Provision the bucket with Object Lock enabled, a default retention, KMS-managed encryption, a TLS-only and least-privilege bucket policy, retention governance, and access logging, mirroring the hardened bucket in the jump-host module. Document the module.
Test scenarios. `terraform validate` passes. The bucket declares Object Lock, KMS encryption, a TLS-only policy, and access logging. The README states the retention model.
Verification. `terraform validate`, module review.

### U4. Record the object location on the index

Goal. Populate the Postgres evidence row with the object location so a reader resolves the object.
Requirements. R5.
Files. `services/review/evidence.py`, `services/integration/mcp_gateway/evidence_reader.py`.
Approach. On persist, write the object reference into the `location` column. Confirm the reader returns the location so a consumer resolves the object from the index.
Test scenarios. A persisted record carries the object location. The reader returns the location for a stored artifact.
Verification. `tests/integration/test_object_store_evidence.py`, `inv test`.
