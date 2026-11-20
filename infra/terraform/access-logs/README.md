# access-logs

Reusable module that provisions the S3 bucket receiving S3 server access logs from every other platform bucket, which supports AU-2 and AU-12 audit generation for object access. Nothing is created unless `create_bucket = true`.

## What it provisions

- An S3 bucket named `bucket_name`, or `<name_prefix>-access-logs-<account id>` when `bucket_name` is empty.
- Versioning, bucket-owner-enforced object ownership, and a public access block that denies all public access.
- SSE-S3 AES256 encryption. S3 server access logging cannot deliver to a bucket that uses SSE-KMS as its default encryption.
- A lifecycle rule that moves logs to Glacier Instant Retrieval after `cold_storage_after_days` and expires them after `retention_days`. The defaults of 365 and 913 days follow the OMB M-21-31 tiers of 12 months active and 18 months cold storage.
- A bucket policy that lets the S3 logging service write objects from this account only and denies non-TLS access.

The bucket does not log access to itself, because a log target that logs to itself produces a record for every record it receives.

## Usage

The [bootstrap](../bootstrap) root creates the bucket before any composition root applies. Each bucket module takes the bucket name as its required `access_log_bucket` input.

```hcl
module "access_logs" {
  source = "../access-logs"

  create_bucket = true
  name_prefix   = "agentic-platform"
  tags          = var.tags
}
```

## Inputs

| Name | Description | Default |
|---|---|---|
| `create_bucket` | Master toggle. | `false` |
| `bucket_name` | Bucket name. Empty derives the name from the prefix and account ID. | `""` |
| `name_prefix` | Prefix for the derived bucket name. | required |
| `cold_storage_after_days` | Days before logs move to Glacier Instant Retrieval. | `365` |
| `retention_days` | Days before logs expire. | `913` |
| `tags` | Tags applied to the created resources. | `{}` |

## Outputs

| Name | Description |
|---|---|
| `bucket_id` | Name of the access-log bucket. |
| `bucket_arn` | ARN of the access-log bucket. |
