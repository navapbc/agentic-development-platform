# langfuse-blob-store

Reusable module that provisions the S3 bucket Langfuse uses for event and media uploads. Langfuse writes each ingested event batch here before the worker processes it into ClickHouse, and stores uploaded media here. MinIO plays this role locally. Nothing is created unless `create_bucket = true`.

## What it provisions

- An S3 bucket with versioning enabled, and a lifecycle rule that expires replaced and deleted versions after `noncurrent_version_retention_days` and aborts incomplete multipart uploads after seven days.
- Server-side encryption, SSE-S3 AES256 by default or SSE-KMS when `kms_key_arn` is set, with a bucket key to reduce KMS cost.
- Bucket-owner-enforced object ownership and a public access block that denies all public access.
- A bucket policy that denies non-TLS access.
- S3 server access logging to the required `access_log_bucket`, normally the bucket the [access-logs](../access-logs) module creates.

## Langfuse settings

Set `LANGFUSE_S3_EVENT_UPLOAD_BUCKET` and `LANGFUSE_S3_MEDIA_UPLOAD_BUCKET` to `bucket_id`, keep the `events/` and `media/` prefixes from `compose.yaml`, and leave the endpoint and access keys unset so the SDK uses the task role and the S3 gateway endpoint.

## Usage

```hcl
module "langfuse_blob_store" {
  source = "../langfuse-blob-store"

  create_bucket     = true
  name_prefix       = "agentic-platform"
  access_log_bucket = var.access_log_bucket
  tags              = var.tags
}
```
