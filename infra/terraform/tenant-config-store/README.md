# tenant-config-store

Reusable module that provisions the bucket that holds each tenant's config bundle, the repositories, runtime profiles, checks, gates, and gate engines a tenant publishes from its own CI. Each tenant owns the prefix `tenants/<tenant>/`. Nothing is created unless `create_tenant_config_store = true`.

## What it provisions

- A bucket named `<name_prefix>-tenant-config-<suffix>` with bucket-owner-enforced ownership, a public-access block, versioning, and server access logging to `access_log_bucket`.
- SSE-KMS under a dedicated customer managed key with yearly automatic rotation and an S3 Bucket Key. The key policy names only the account, and the tenant roles receive the key through IAM policies limited to the S3 service, so the key policy holds no role that may not exist yet.
- The platform finding exceptions file `platform/image_exceptions.yaml`, created once as `config_version: 1` with `exceptions: []`. The object ignores later changes to its content, so an apply never overwrites the entries the `image-exception` workflow and the daily review write with conditional puts.
- When `publish_image_policy` is true, `platform/image_policy.yaml` with the content of `image_policy`, which the exception writers outside the cluster read to check the scope of each entry.
- An IAM policy, output as `platform_exceptions_read_policy_arn`, that reads `platform/image_exceptions.yaml` for each tenant worker's pull gate.
- A lifecycle rule that keeps replaced versions for `noncurrent_version_retention_days`, 365 by default, which is how far back a tenant's config history reaches in the bucket, and expires abandoned multipart uploads after 7 days.
- A bucket policy that denies the following.
  - Any request without TLS, and any request below TLS 1.2.
  - Any object written outside `tenants/` and the two platform keys.
  - A write or delete under `tenants/<tenant>/` by a principal other than `admin_principals` and those tagged `tenant-config-publisher = <tenant>`.
  - A read under `tenants/<tenant>/` by a principal other than `shared_readers`, `exception_reviewers`, `admin_principals`, and those tagged `tenant-config-reader = <tenant>` or `tenant-config-publisher = <tenant>`, so one tenant's worker cannot read another tenant's bundle even with a broader IAM policy.
  - A read by an `exception_reviewers` principal of any object other than `tenants/<tenant>/image_exceptions.yaml` and the `platform/` keys.
  - Any upload that names an encryption other than SSE-KMS, or a KMS key other than the bucket key.

The two tenant denies are fixed statements that resolve the tenant from the caller's principal tag, `${aws:PrincipalTag/tenant-config-reader}` and `${aws:PrincipalTag/tenant-config-publisher}` in their `NotResource`, so the policy does not grow with the tenant count and stays far below the 20 KB limit of a bucket policy. A principal without the tag matches neither entry and is denied every tenant prefix. The `tenant_principal_tag_keys` output names both keys. A tenant's worker role carries the reader tag and its publisher role the publisher tag, each with the tenant name as its value, and a principal that reads every tenant, such as repository intelligence or the inbound webhook, is listed in `shared_readers` instead. The denies also cover keys outside `tenants/` and `platform/`, where the bucket admits no object.

The tags decide which tenant a principal reaches, so grant `iam:TagRole` and `iam:UntagRole` on the two keys to administrators only, and let no role that reads the bucket accept them as session tags. EKS Pod Identity sets only its own `kubernetes-*` and `eks-*` session tags, and the GitHub OIDC publisher roles do not allow `sts:TagSession`.

The shared readers, reviewers, and admins appear only in policy conditions, so the bucket can be created before the roles. The [tenant](../tenant) module grants each tenant's worker and publisher the matching IAM permissions and tags the publisher role.

## FIPS 140 and FedRAMP

S3 encrypts with AWS-LC, and the data keys are protected by the AWS KMS HSM, as the [rebar-ticket-store](../rebar-ticket-store/README.md#fips-140-and-fedramp) README describes. The module supports CM-3 and CM-5, because every change to a bundle is a new object version written by the tenant's publisher role, and AU-2 and AU-12 when the bucket is passed to the `cloudtrail` module's `data_event_bucket_arns`. It also supports SC-28, SC-8, and AC-3 through the tenant prefix denies.

## Tests

`tests/tenant_config_store.tftest.hcl` plans the module against a mocked provider and asserts on the bucket settings, the tag-based tenant denies, and that the policy for 200 tenants holds eight fixed statements and no tenant name. Run it with `inv terraform-test`.

## Teardown

The bucket is versioned and cannot be deleted while it holds any object version. A principal in `admin_principals` empties it, every version and delete marker included, before `terraform destroy`.

## Usage

```hcl
module "tenant_config_store" {
  source = "../tenant-config-store"

  create_tenant_config_store = true
  name_prefix                = "agentic-platform"
  access_log_bucket          = "agentic-platform-access-logs-111111111111"
  admin_principals           = ["arn:aws:iam::111111111111:role/platform-admin"]
  tenants                    = ["adp", "payments"]
  shared_readers             = ["arn:aws:iam::111111111111:role/agentic-platform-repo-intelligence-task"]
}
```
