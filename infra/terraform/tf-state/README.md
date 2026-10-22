# tf-state

Reusable module that provisions the S3 bucket holding Terraform state for the platform's composition roots. State records every managed resource and can hold secret material such as generated passwords, so it is stored encrypted, versioned, and reachable only over TLS rather than on a workstation. Nothing is created unless `create_state_bucket = true`.

## What it provisions

- An S3 bucket with versioning enabled, so a state file can be restored after a bad apply.
- A lifecycle rule that expires superseded state versions after `noncurrent_version_retention_days` and aborts incomplete multipart uploads after seven days.
- Server-side encryption, SSE-S3 AES256 by default or SSE-KMS when `kms_key_arn` is set, with a bucket key to reduce KMS cost.
- Bucket-owner-enforced object ownership and a public access block that denies all public access.
- A bucket policy that denies non-TLS access.
- S3 server access logging to the required `access_log_bucket`, normally the bucket the [access-logs](../access-logs) module creates.

## Locking

The composition roots lock state with S3 native locking through `use_lockfile = true`, which writes a lock object beside the state file. It needs Terraform 1.10 or later and no DynamoDB table.

## Usage

Apply the module once through the [bootstrap](../bootstrap) root, which keeps its own small state locally because the bucket it creates cannot store the state that creates it.

```hcl
module "tf_state" {
  source = "../tf-state"

  create_state_bucket = true
  name_prefix         = "agentic-platform"
  kms_key_arn         = module.kms.key_arn
  access_log_bucket   = module.access_logs.bucket_id
  tags                = var.tags
}
```
