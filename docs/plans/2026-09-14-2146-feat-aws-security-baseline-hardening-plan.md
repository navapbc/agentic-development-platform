---
title: AWS Security Baseline Hardening - Plan
type: feat
date: 2026-09-14
origin: docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-brainstorm
execution: code
---

# AWS Security Baseline Hardening - Plan

## Goal Capsule

Objective. The platform selects FIPS service endpoints on every AWS client and records control-plane activity to a multi-region CloudTrail, closing the two platform-owned baseline gaps the FedRAMP hardening design names.

Means. Add a FIPS-endpoint setting that the AWS clients honor through the botocore `use_fips_endpoint` configuration, and make the CloudTrail module multi-region through a default-on variable.

Product authority. This plan enriches the FedRAMP readiness scope of the roadmap at [2026-09-02-1801-feat-remaining-work-roadmap-plan.md](2026-09-02-1801-feat-remaining-work-roadmap-plan.md). It implements two platform-owned controls the hardening design names.

Stop conditions. AWS clients resolve FIPS endpoints when the setting is on, and the CloudTrail trail is multi-region. No new AWS resource type is introduced.

## Product Contract

### Summary

The hardening design records two platform-owned baseline gaps. SC-13 cryptographic protection is inherited at the module level from AWS FIPS-validated cryptography, but the platform does not select FIPS service endpoints on its clients, so a call can traverse a non-FIPS endpoint. CloudTrail is single-region in the `cloudtrail` module, so control-plane activity in other regions is not captured. This plan selects FIPS endpoints on the AWS clients and makes the trail multi-region.

### Problem Frame

FedRAMP Medium expects cryptographic operations to traverse FIPS-validated endpoints and expects audit coverage of control-plane activity. botocore resolves FIPS endpoints when `use_fips_endpoint` is set, and CloudTrail captures every region when the trail is multi-region. Both are configuration changes on controls the platform already partly implements.

### Requirements

- R1. A setting selects FIPS service endpoints, and the AWS clients the platform builds honor it.
- R2. The setting defaults to on for a deployed environment and is overridable for local development where a FIPS endpoint is not reachable.
- R3. The CloudTrail trail is multi-region through a default-on variable in the `cloudtrail` module.

### Scope Boundaries

- This plan configures FIPS endpoints and a multi-region trail. It does not replace the cryptographic modules, which are inherited from AWS.
- This plan does not add new AWS resource types beyond the multi-region trail setting.

### Sources

- Roadmap and hardening design: [2026-09-02-1801-feat-remaining-work-roadmap-plan.md](2026-09-02-1801-feat-remaining-work-roadmap-plan.md), [fedramp-medium-hardening-design.md](../fedramp-medium-hardening-design.md).
- AWS client construction: `services/review/evidence.py` `ObjectStoreWriter`, `scripts/evidence_bucket_bootstrap.py`, and any other boto3 client builder.
- CloudTrail module: `infra/terraform/cloudtrail/main.tf` (`is_multi_region_trail`).

## Planning Contract

### Key Technical Decisions

- KTD1. Select FIPS endpoints through the botocore `use_fips_endpoint` config, not by hardcoding endpoint URLs. Governs R1. Rationale. botocore resolves the correct FIPS endpoint per service and region, so the config is portable and correct.
- KTD2. Default FIPS on and allow an override. Governs R2. Rationale. A deployed environment requires FIPS, and local development against MinIO has no FIPS endpoint, so the override keeps local development working.
- KTD3. Make the trail multi-region through a default-on variable. Governs R3. Rationale. A variable keeps the module reusable while defaulting to the compliant posture.

### High-Level Design

A `use_fips_endpoints` setting lands in `scripts/config.py` with a default-on value and an `.env.example` entry. A shared boto3 client builder applies the botocore config with `use_fips_endpoint` when the setting is on, and the object-store writer and the bootstrap script build their clients through it. The CloudTrail module gains an `is_multi_region_trail` variable defaulting to true, wired into the trail resource. A unit test asserts the client config carries the FIPS flag when enabled and omits it when disabled.

### Sequencing

U1 adds the FIPS setting and the shared client builder. U2 makes CloudTrail multi-region. The units are independent.

## Implementation Units

### U1. Select FIPS endpoints on the AWS clients

Goal. Build every AWS client with FIPS endpoint resolution when the setting is on.
Requirements. R1, R2.
Files. `scripts/config.py`, a shared boto3 client builder helper, `services/review/evidence.py`, `scripts/evidence_bucket_bootstrap.py`, `.env.example`.
Approach. Add a `use_fips_endpoints` setting defaulting to on. Add a client builder that applies a botocore `Config(use_fips_endpoint=True)` when the setting is on and passes the endpoint, region, and credentials. Route the object-store writer and the bootstrap client through the builder. Allow the setting off for local development.
Test scenarios. With the setting on, the built client config carries the FIPS flag. With it off, the flag is absent. The endpoint, region, and credentials pass through unchanged.
Verification. `tests/unit/test_object_store_evidence.py` additions or a client-builder test, `inv test`.

### U2. Make CloudTrail multi-region

Goal. Capture control-plane activity across regions.
Requirements. R3.
Files. `infra/terraform/cloudtrail/main.tf`, `infra/terraform/cloudtrail/variables.tf`.
Approach. Add an `is_multi_region_trail` variable defaulting to true and wire it into the trail resource, replacing the hardcoded false.
Test scenarios. `terraform validate` passes. The trail declares multi-region by default. The variable can override it for a single-region deployment.
Verification. `terraform validate`, module review.
