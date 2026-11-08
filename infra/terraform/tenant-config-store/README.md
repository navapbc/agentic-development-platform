# tenant-config-store

Reusable module that provisions the bucket that holds each tenant's config bundle, the repositories, runtime profiles, checks, gates, and gate engines a tenant publishes from its own CI. Each tenant owns the prefix `tenants/<tenant>/`. Nothing is created unless `create_tenant_config_store = true`.

## What it provisions

- A bucket named `<name_prefix>-tenant-config-<suffix>` with bucket-owner-enforced ownership, a public-access block, versioning, and server access logging to `access_log_bucket`.
- SSE-KMS under a dedicated customer managed key with yearly automatic rotation and an S3 Bucket Key. The key policy names only the account, and the tenant roles receive the key through IAM policies limited to the S3 service, so the key policy holds no role that may not exist yet.
- A lifecycle rule that keeps replaced versions for `noncurrent_version_retention_days`, 365 by default, which is how far back a tenant's config history reaches in the bucket, and expires abandoned multipart uploads after 7 days.
- A bucket policy that denies the following.
  - Any request without TLS, and any request below TLS 1.2.
  - Any object written outside `tenants/`.
  - A write or delete under a tenant's prefix by a principal other than that tenant's `publishers` and `admin_principals`.
  - A read under a tenant's prefix by a principal other than that tenant's `readers`, `publishers`, and `admin_principals`, so one tenant's worker cannot read another tenant's bundle even with a broader IAM policy.
  - Any upload that names an encryption other than SSE-KMS, or a KMS key other than the bucket key.

The tenant principals appear only in policy conditions, so the bucket can be created before the roles. The [tenant](../tenant) module grants each tenant's worker and publisher the matching IAM permissions.

## FIPS 140 and FedRAMP

S3 encrypts with AWS-LC, and the data keys are protected by the AWS KMS HSM, as the [rebar-ticket-store](../rebar-ticket-store/README.md#fips-140-and-fedramp) README describes. The module supports CM-3 and CM-5, because every change to a bundle is a new object version written by the tenant's publisher role, and AU-2 and AU-12 when the bucket is passed to the `cloudtrail` module's `data_event_bucket_arns`. It also supports SC-28, SC-8, and AC-3 through the per-prefix denies.

## Tests

`tests/tenant_config_store.tftest.hcl` plans the module against a mocked provider and asserts on the bucket settings and the per-prefix denies. Run it with `inv terraform-test`.

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
  tenants = {
    adp = {
      publishers = ["arn:aws:iam::111111111111:role/agentic-platform-tenant-adp-publisher"]
      readers    = ["arn:aws:iam::111111111111:role/agentic-platform-eks-tenant-adp"]
    }
  }
}
```
