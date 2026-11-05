---
title: Rebar Ticket Store Bucket Terraform - Plan
type: feat
date: 2026-09-14
origin: docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-brainstorm
execution: infra
---

# Rebar Ticket Store Bucket Terraform - Plan

## Goal Capsule

Objective. The rebar ticket store syncs to an S3 bucket that is encrypted under FIPS-validated cryptography, reachable only over TLS, versioned, isolated from the evidence and audit stores, and readable and writable only by distinct reader and writer roles, so the ticket store conforms to the platform-owned FedRAMP Medium controls.

Means. Add a toggleable `rebar-ticket-store` module that provisions the bucket, a dedicated KMS key, and the reader and writer roles, or accepts existing roles. Install the `git-remote-s3` helper that rebar uses for an `s3://` remote, point the store's fetch and push URLs at the reader and writer roles, validate the isolation contract at worker startup, and give local development the same remote against MinIO.

Product authority. This plan enriches the FedRAMP readiness scope of the roadmap at [2026-09-02-1801-feat-remaining-work-roadmap-plan.md](2026-09-02-1801-feat-remaining-work-roadmap-plan.md). It provisions the rebar ticket bucket target the local-to-AWS mapping names.

Stop conditions. The module applies in a sandbox account. A store pushed under the writer role is fetched under the reader role. The reader cannot push, a principal outside the allowlist is denied, and a request without TLS 1.2 is denied. The worker refuses to start on an isolation violation. The same remote works locally against MinIO. `terraform validate`, `inv ci`, and `inv sast` pass.

## Product Contract

### Summary

The rebar backend keeps the ticket store in a git repository, and rebar syncs that repository to S3 through the awslabs `git-remote-s3` helper, which stores each ref as git bundles under a bucket prefix. No terraform provisions the bucket, the platform does not install the helper, the reader and writer roles the tickets configuration names do not exist, and the isolation loader is not called. This plan provisions the bucket and roles, installs and configures the helper, wires the loader, and gives local development the same remote.

### Problem Frame

The ticket store holds work items and their status. FedRAMP Medium expects SC-13 FIPS-validated cryptography, SC-28 encryption at rest, SC-8 protected transmission, SC-7 isolation, and AC-6 least privilege. The platform already documents that the read and write roles are distinct and that the store never reuses the evidence or audit bucket or writer roles, but nothing enforces it. AWS provides the bucket and key controls as configuration, so the platform's responsibility is the terraform, the helper configuration, and the startup validation.

### Helper behavior the bucket must support

`git-remote-s3` 0.3.2 or later, which `nava-rebar[s3]` requires, makes these S3 calls, read from its source.

- Fetch: `ListObjectsV2` and `GetObject`.
- Push: `PutObject` of a new `<sha>.bundle`, then `DeleteObject` of the previous bundle.
- Per-ref lock: `PutObject` of `LOCK#.lock` with `If-None-Match: *`, `HeadObject` to test a lock for staleness, and `DeleteObject` to release it.
- It sends no server-side encryption headers, so the bucket's default encryption applies, and a policy that requires an encryption header would break every push.
- It reads the AWS profile from the remote URL, `s3://<profile>@<bucket>/<prefix>`, and builds its client from boto3, so it honors `AWS_USE_FIPS_ENDPOINT` and a profile's `endpoint_url`.

Every push deletes the bundle it replaces, which rules out Object Lock and makes versioning plus a noncurrent-version lifecycle the recovery path.

### Requirements

- R1. A terraform module provisions the ticket bucket with SSE-KMS under a dedicated customer managed key with automatic rotation, an S3 Bucket Key, bucket-owner-enforced ownership, a public-access block, versioning, and server access logging to `access_log_bucket`.
- R2. The bucket policy denies any request without TLS, any request below TLS 1.2, any object read, write, delete, or restore by a principal outside the reader, writer, and admin allowlist, any write or delete by the reader role, and any `PutObject` that names an encryption other than SSE-KMS or a KMS key other than the bucket key.
- R3. The key policy allows the reader to decrypt and the writer to decrypt and generate data keys only through S3 for this bucket.
- R4. The module creates the reader and writer roles by default, trusted by `trusted_principals`. It accepts existing reader and writer role ARNs in their place, and then creates no roles. The two roles are always distinct.
- R5. `admin_principals` names the principals that may read, write, and delete objects for seeding and teardown. It is empty by default.
- R6. Noncurrent bundle versions expire after `noncurrent_version_retention_days`, 90 by default, and incomplete multipart uploads after 7 days.
- R7. The module is toggleable, so an account that reuses an existing ticket bucket provisions nothing.
- R8. The platform installs `nava-rebar[s3]`, so `git-remote-s3` is present in the worker image and the host environment.
- R9. An operator task configures the store's `remote.origin.url` with the reader profile and `remote.origin.pushurl` with the writer profile from `tickets.yaml`, so fetches and pushes run under different roles without a rebar change.
- R10. The activity worker validates the tickets store isolation contract at startup when the rebar backend has an S3 remote, and refuses to start on a violation. The check also confirms that the store's fetch and push URLs name the configured bucket and prefix.
- R11. Local development syncs the same store to a MinIO bucket through local reader and writer profiles that name MinIO as their `endpoint_url`, with the helper's conditional-write lock working against MinIO.

### FIPS 140 and FedRAMP

- At rest. S3 performs SSE-KMS encryption with AWS-LC, which AWS lists among its FIPS-validated modules for every S3 server-side encryption mode. The data key is protected by the AWS KMS HSM, which holds a FIPS 140-3 validation. NIST moved the remaining FIPS 140-2 certificates to the historical list in September 2026, so FIPS 140-3 is the current validation to cite.
- In transit. The bucket policy requires TLS 1.2 or later. FIPS-validated TLS needs the client to use the `s3-fips` endpoint, which botocore selects when `AWS_USE_FIPS_ENDPOINT=true`, the setting the platform already exports for every AWS SDK path. No bucket policy condition can require the FIPS endpoint, so the deployed configuration and the U4 verification carry that control. `s3-fips` accepts only virtual-hosted-style addressing, and the bucket name contains no dots, so that holds.
- Authorization. S3 and KMS are within the FedRAMP authorization of the hosting account. The controls this plan implements are SC-13, SC-28, SC-8, SC-7, AC-6, AU-2 through server access logging and CloudTrail data events where the account enables them, and CP-9 through versioning.

### Scope Boundaries

- The module provisions the bucket, key, and roles. It does not initialize the rebar store, which the rebar backend owns.
- Distributing AWS profiles for the reader and writer roles to ECS tasks belongs to the container-platform services feature. This feature documents the profile shape and verifies it from a workstation.
- The evidence bucket is a separate module. This bucket stays isolated from it by the bucket policy and by the startup validation.

### Sources

- Roadmap and mapping: [2026-09-02-1801-feat-remaining-work-roadmap-plan.md](2026-09-02-1801-feat-remaining-work-roadmap-plan.md), [local-to-aws-mapping.md](../local-to-aws-mapping.md).
- Credential isolation: `docs/tickets-store-credentials.md`, `services/integration/tickets/store_config.py`.
- Helper: the awslabs `git-remote-s3` repository, Apache-2.0, `git_remote_s3/remote.py`.
- FIPS: the AWS FIPS 140-3 compliance page.
- Hardened-bucket pattern: `infra/terraform/evidence-store`, `infra/terraform/langfuse-blob-store`.

## Planning Contract

### Key Technical Decisions

- KTD1. Mirror the hardened-bucket pattern from the evidence-store and langfuse-blob-store modules. Governs R1, R7. Rationale. The pattern already establishes encryption, TLS-only access, the public-access block, ownership, logging, and versioning, so reuse keeps the posture consistent.
- KTD2. Use a dedicated customer managed key rather than SSE-S3. Governs R1, R3. Rationale. A key policy scoped to the two roles and to S3 for this bucket enforces least privilege independent of the bucket policy, and CloudTrail records each key use.
- KTD3. Create the roles by default and accept existing ARNs. Governs R4. Rationale. Nothing else in the repository defines them and the tickets configuration needs their ARNs, while an organization that issues roles centrally must be able to supply its own.
- KTD4. Split fetch and push with git's `url` and `pushurl` for one remote. Governs R9. Rationale. The same activity worker polls and writes back, and the helper takes the profile from the URL, so git selects the reader for every fetch and the writer for every push without a change to rebar.
- KTD5. Allowlist object access rather than deny named roles, and leave bucket-level actions to IAM. Governs R2. Rationale. Denying object access outside the reader, writer, and admins excludes the evidence and audit writers by construction and covers roles that do not exist yet. HeadBucket is authorized as `s3:ListBucket`, and AWS provider 6.70 treats a denied HeadBucket as a deleted bucket and plans a replacement, which the sandbox verification reproduced, so the policy does not deny listing.
- KTD6. Recover through versioning and a 90-day noncurrent lifecycle rather than Object Lock. Governs R6. Rationale. The helper deletes the replaced bundle on every push, and git history already holds every ticket event, so old versions only need to cover recovery from a bad push or a lossy repair.
- KTD7. Wire the existing isolation loader at worker startup. Governs R10. Rationale. The loader and its tests exist, and the documentation already states the contract is validated at load time.
- KTD8. Reach MinIO locally through an `endpoint_url` in the two local profiles, loaded by the activity worker through `AWS_CONFIG_FILE`. Governs R11. Rationale. A profile-scoped endpoint reaches only the clients built for those profiles, so the evidence store and any other S3 client in the worker keep their own endpoints, and the local remote URL keeps the deployed shape. `AWS_ENDPOINT_URL_S3` would redirect every S3 client in the process.

### High-Level Design

A `rebar-ticket-store` module creates the KMS key, the bucket, and, unless existing ARNs are supplied, the reader and writer roles with inline policies limited to the bucket prefix and the key. The bucket policy denies non-TLS and pre-1.2 TLS requests, denies object access outside the allowlist, denies writes by the reader, and denies a foreign KMS key on `PutObject`. The module emits the bucket, the remote URL shape, the role ARNs, and the key ARN.

The composition root wires the module behind `create_rebar_ticket_store`, reaches S3 through the existing gateway endpoint, and documents the seed and teardown steps.

On the platform side, `nava-rebar[s3]` installs the helper. `inv rebar-store-remote` reads `tickets.yaml` and sets the store's fetch and push URLs to `s3://<reader profile>@<bucket>/<prefix>` and `s3://<writer profile>@<bucket>/<prefix>`. The activity worker calls `load_tickets_store_config` at startup when the store remote is an S3 URL, then compares the store's remote URLs with the configuration. Locally, compose creates a MinIO bucket for the store, and the activity worker loads two local profiles that carry the MinIO credentials and endpoint. rebar's repair of a ref that holds two bundles resolves the remote through its fetch URL, so in AWS it runs as the reader and is denied. The helper's lock prevents that state, and the module README documents the manual repair under the writer URL.

### Sequencing

U1 adds the module. U2 wires it into the composition root and depends on U1. U3 wires the platform and is independent of U1. U5 adds the local MinIO path and depends on U3. U4 verifies the deployed path in a sandbox account and depends on U1, U2, and U3.

## Implementation Units

### U1. Rebar ticket store module

Goal. Provision the bucket, key, and roles with the controls above.
Requirements. R1, R2, R3, R4, R5, R6, R7.
Files. `infra/terraform/rebar-ticket-store/` (new module with versions, variables, main, iam, outputs, README).
Approach. Require Terraform 1.11 or later and AWS provider 6.50 or later below 7, the constraints every module uses. Create the key with rotation and a policy scoped by `kms:ViaService` and the `aws:s3:arn` encryption context. Create the bucket with ownership, versioning, the public-access block, SSE-KMS with a Bucket Key, logging, and the lifecycle rules. Write the bucket policy statements of R2. Create the roles and their policies unless `existing_reader_role_arn` and `existing_writer_role_arn` are set, with a precondition that both or neither are set and that they differ.
Test scenarios. `terraform validate` passes. Mocked-provider plans cover the disabled module, created roles, supplied roles, and a supplied pair that is equal, which fails the precondition. Rendered policies show the TLS, allowlist, reader-write, and foreign-key denies.
Verification. `terraform validate`, `terraform test` with mocked providers, module review.

### U2. Wire the ticket bucket into the composition root

Goal. Make the composition root provision the ticket store optionally.
Requirements. R7.
Files. `infra/terraform/examples/private-bedrock/` (data-tier.tf, variables, outputs, tfvars examples, README), `README.md` Deploying to AWS and teardown sections.
Approach. Add the module block behind `create_rebar_ticket_store`, pass `trusted_principals`, `admin_principals`, and the existing-role inputs, and surface the bucket, remote URL, and role ARNs. Add `create_rebar_ticket_store = false` to the service toggles in both tfvars examples. Add a deploy step that seeds the store under the writer role, and extend the teardown step that empties versioned buckets to cover this bucket.
Test scenarios. `terraform validate` passes with the store enabled and disabled. The outputs appear when enabled. A plan with the store disabled leaves the deployed resources unchanged.
Verification. `terraform validate`, mocked-provider root plans.

### U3. Platform wiring

Goal. Install the helper, configure the split remote, and enforce the isolation contract.
Requirements. R8, R9, R10.
Files. `pyproject.toml`, `uv.lock`, `tasks.py`, a `scripts/rebar_store_remote.py` script, `services/integration/tickets/store_config.py`, the activity worker startup, `config/local/tickets.yaml.example`, `docs/tickets-store-credentials.md`, `ROADMAP.md`, tests.
Approach. Add the `s3` extra to the `nava-rebar` requirement in the `review` extra, which the `worker` extra includes, so the image carries the helper, and lock it. Add `store.reader_profile` and `store.writer_profile` to the tickets configuration. Add `inv rebar-store-remote`, which runs in the activity worker through the same scripted-task path as the other rebar operator tasks and on the host with `--local`. Call the loader from the activity worker startup preflights, beside the signing and evidence preflights, when the store remote is an S3 URL, and fail when the configured bucket and prefix differ from the remote URLs. Correct the tickets store credentials document, and remove `load_tickets_store_config` from the unwired-feature removal item on the roadmap.
Test scenarios. The task writes the expected `url` and `pushurl`. The preflight passes on a consistent configuration and fails on each isolation violation, on a remote that names another bucket, and on equal profiles. A store without an S3 remote skips the check. `inv sast` passes with the new dependency.
Verification. `inv ci`, `inv sast`, `basedpyright`.

### U4. Sandbox verification

Goal. Prove the deployed path end to end.
Requirements. R1 through R10.
Files. None beyond fixes found during the run.
Approach. Apply the composition root with the store enabled and `trusted_principals` set to the operator. Seed a store, push under the writer profile, and fetch a fresh clone under the reader profile, both with `AWS_USE_FIPS_ENDPOINT=true`. Confirm that a push under the reader is denied, that an unlisted principal is denied, that plain HTTP and TLS 1.1 are denied, that two concurrent pushes serialize on the lock, that a noncurrent bundle restores, and that FIPS endpoint traffic leaves through the S3 gateway endpoint from the jumphost subnet. Then tear down with the README steps.
Test scenarios. Each check above passes, and the CloudTrail and access logs record the denied requests.
Verification. The commands and results recorded in the PR description.

### U5. Local MinIO parity

Goal. Run the same split remote against MinIO locally.
Requirements. R11.
Files. `compose.yaml`, `.gitignore`, `config/local/rebar-store-aws-config.example`, `config/local/tickets.yaml.example`, `README.md`.
Approach. Create the `rebar-tickets` bucket in the MinIO startup command, ship `config/local/rebar-store-aws-config.example` with reader and writer profiles that carry the MinIO credentials and `endpoint_url = http://minio:9000`, and set `AWS_CONFIG_FILE` on the activity worker to the copy. The MinIO release in compose, `RELEASE.2026-07-17T12-07-51Z`, rejects a second `PutObject` with `If-None-Match: *` with `PreconditionFailed`, so the helper's lock works.
Test scenarios. A local push and a fresh local fetch succeed, a second concurrent push waits on the lock, and the evidence store path against MinIO is unchanged.
Verification. A local round trip recorded in the PR description, `inv ci`.

## Verification Record

The sandbox verification ran on 2026-10-28 in us-east-1 with the full composition root, Terraform 1.11.4, and AWS provider 6.70.0. Each check used the platform's `configure_store_remote` and `check_store_remote` and the `git-remote-s3` 0.4.2 helper, with `AWS_USE_FIPS_ENDPOINT=true`.

| Check | Result |
|---|---|
| Profiles assume the created roles | Pass |
| Seed push under the writer profile | Pass |
| rebar's own ticket write pushes through the writer push URL | Pass |
| Fresh clone under the reader profile | Pass |
| Push and delete under the reader profile | Denied |
| Object read, write, and delete by an administrator outside `admin_principals` | Denied by the bucket policy |
| Plain HTTP request | Denied by the bucket policy |
| TLS 1.1 handshake | Refused |
| Upload with SSE-S3 or another KMS key | Denied by the bucket policy |
| Two concurrent writer pushes | One succeeded, the other failed with `PreconditionFailed`, one bundle remained |
| Restore of a replaced bundle from its noncurrent version | Pass, restored object encrypted with `aws:kms` |
| `s3-fips` from the jumphost subnet | Every resolved address is in the S3 gateway prefix list, and HTTPS reached S3 |
| Second plan after apply | No changes |

The run found two defects that the plan-time tests could not observe, and both are fixed in the module. The encryption denies first used `StringNotEqualsIfExists`, which denies every upload without an encryption header, so they now require the header to be present. The object deny first covered `s3:ListBucket*`, and a later plan by a principal outside the allowlist dropped the bucket from state and planned a new one, so listing now stays with IAM.

A second run on 2026-10-29 verified the follow-up changes. The bucket, key, and role policies are rendered with `jsonencode`, and `tests/rebar_ticket_store.tftest.hcl` asserts on them at plan time and fails when either defect above is reintroduced. CI runs it through `inv terraform-test`. The trail records object-level data events on the store bucket, and within 15 minutes it delivered events for the reader's `GetObject` and denied `DeleteObject` and for the writer's `PutObject` calls, including the upload denied for naming SSE-S3, each with its role and `errorCode`. The activity worker in the local stack, with the store syncing to MinIO, started when its remote matched `tickets.yaml`, refused to start with a `ConfigError` when the push URL named the reader profile, and started again after `inv rebar-store-remote` restored the remote. That task runs in a one-off container, so it works while the worker refuses to start. rebar's repair of a ref with two bundles still resolves the fetch URL, which is reported upstream as navapbc/rebar#145.

A plan against the deployed stack with `create_rebar_ticket_store = false` destroys only the 14 store resources and updates the trail's event selectors in place, so the toggle reaches nothing else. Two concurrent pushes from the local activity worker to MinIO left one bundle, and the second push failed as stale and had to fetch and retry.

The server access logs for the second-run bucket arrived about an hour after the requests and include the denied `REST.PUT.OBJECT` with `403 AccessDenied`, so both the access logs and the CloudTrail data events record denied requests. CloudTrail delivers within minutes and is the audit record. The access logs are best-effort and supplement it.
