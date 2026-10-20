# evidence-store

Reusable module that provisions the S3 bucket for the evidence and audit object store under a write-once-read-many retention model. It exists so the compliance evidence tier is managed as shared infrastructure and can be reused or skipped when one already exists. Nothing is created unless `create_evidence_store = true`.

The bucket enables S3 Object Lock with a default retention, so an evidence object cannot be deleted or overwritten within its window. The same guarantee is exercised locally against MinIO Object Lock before this AWS mapping applies, so the local path proves the deployed behavior.

## What it provisions

- An S3 bucket with Object Lock enabled and versioning enabled, which Object Lock requires.
- A default Object Lock retention of `retention_days` in `object_lock_mode`. COMPLIANCE forbids any early delete or overwrite, including by the account root.
- Server-side encryption, SSE-S3 AES256 by default or SSE-KMS when `kms_key_arn` is set, with a bucket key to reduce KMS cost.
- A public access block that denies all public access.
- A bucket policy that denies non-TLS access and restricts writes to the account root and the `writer_principal_list`.
- S3 server access logging to the required `access_log_bucket`, normally the bucket the [access-logs](../access-logs) module creates.

## Retention model

The application writes each evidence object under a per-object Object Lock retention. The bucket default retention here is the floor a deployment enforces even if an object is written without an explicit retention. Set `retention_days` to the compliance baseline the deployment requires, matching the application's `EVIDENCE_RETENTION_DAYS`. The application applies `EVIDENCE_RETENTION_DAYS` to every record and to the per-object lock window. Retention is expressed in days end to end.

## Audit objects

Audit events persist to the same bucket under the `audit/` prefix, keyed by the event id, alongside evidence objects under the `evidence/` prefix. The Postgres tables remain the queryable index, and the object store is the tamper-evident tier.

## Local development

The evidence object store is off by default. To exercise it locally against MinIO, set `EVIDENCE_OBJECT_STORE_ENABLED=true`, `EVIDENCE_OBJECT_STORE_ENDPOINT_URL`, `EVIDENCE_OBJECT_STORE_BUCKET`, and the MinIO credentials in `.env`, then run `inv evidence-bucket-bootstrap` once to create the bucket with Object Lock and a default retention. The worker refuses to start when the store is enabled but the bucket is missing or unlocked.

## Usage

```hcl
module "evidence_store" {
  source = "../evidence-store"

  create_evidence_store = true
  name_prefix           = "agentic"
  object_lock_mode      = "COMPLIANCE"
  retention_days        = 2555
  kms_key_arn           = module.kms.key_arn
  writer_principal_list = [aws_iam_role.workers.arn]
  access_log_bucket     = module.access_logs.bucket_id
  tags                  = var.tags
}
```
