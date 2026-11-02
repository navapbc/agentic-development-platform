# rebar-ticket-store

Reusable module that provisions the S3 bucket the rebar ticket store syncs to. rebar keeps its tickets in a git repository and pushes it through the awslabs `git-remote-s3` helper, which stores each ref as git bundles under a key prefix. Nothing is created unless `create_rebar_ticket_store = true`.

## What it provisions

- A bucket named `<name_prefix>-rebar-tickets-<suffix>` with bucket-owner-enforced ownership, a public-access block, versioning, and server access logging to `access_log_bucket`.
- SSE-KMS under a dedicated customer managed key with yearly automatic rotation and an S3 Bucket Key. The key policy lets the store roles and `admin_principals` use the key only through S3 and only for this bucket.
- A lifecycle rule that expires replaced bundle versions after `noncurrent_version_retention_days`, 90 by default, and abandoned multipart uploads after 7 days.
- A reader role that lists and reads the prefix, and a writer role that also writes and deletes it, both trusted by `trusted_principals`. Set `existing_reader_role_arn` and `existing_writer_role_arn` to use roles the organization issues, and the module creates none.
- A bucket policy that denies the following.
  - Any request without TLS, and any request below TLS 1.2.
  - Any object read, write, delete, or restore by a principal other than the reader, the writer, and `admin_principals`. The evidence and audit writers are excluded by construction. Listing stays with IAM, because HeadBucket is authorized as `s3:ListBucket` and the AWS provider treats a denied HeadBucket as a deleted bucket, plans a new one, and leaves the store behind. A listing shows only ref names and bundle hashes.
  - Any write or delete by the reader.
  - Any upload that names an encryption other than SSE-KMS, or a KMS key other than the bucket key. The helper sends no encryption header, so the bucket default applies.

## Remote URLs

`git-remote-s3` takes the AWS profile from the remote URL, `s3://<profile>@<bucket>/<prefix>`. The store's remote fetches with the reader profile and pushes with the writer profile.

```
git -C <store> remote add origin s3://rebar-tickets-reader@<bucket>/<prefix>
git -C <store> remote set-url --push origin s3://rebar-tickets-writer@<bucket>/<prefix>
```

`inv rebar-store-remote` sets both from `config/<env>/tickets.yaml`, and the activity worker refuses to start when the remote does not match that file. Each profile assumes its role. On a workstation that is `source_profile` plus `role_arn`, and in an ECS task it is `credential_source = EcsContainer` plus `role_arn`.

```ini
[profile rebar-tickets-reader]
role_arn = <reader_role_arn output>
source_profile = agentic-platform

[profile rebar-tickets-writer]
role_arn = <writer_role_arn output>
source_profile = agentic-platform
```

When a push finds two bundles for one ref, rebar merges them and replaces the bundles ([navapbc/rebar#145](https://github.com/navapbc/rebar/issues/145)). That repair resolves the remote through its fetch URL, so it runs as the reader, the bucket denies it, and the push stays pending. The helper's per-ref lock prevents the condition, so it follows only an interrupted push. To repair it, set the fetch URL to the writer URL with `git -C <store> remote set-url origin s3://rebar-tickets-writer@<bucket>/<prefix>`, push once, and restore the reader URL with `inv rebar-store-remote`.

## FIPS 140 and FedRAMP

S3 encrypts with AWS-LC, which AWS lists among its FIPS-validated modules for every server-side encryption mode, and the data keys are protected by the AWS KMS HSM, which holds a FIPS 140-3 validation. The bucket policy enforces TLS 1.2 or later. FIPS-validated TLS needs the client to use the `s3-fips` endpoint, which botocore and the helper select when `AWS_USE_FIPS_ENDPOINT=true`. No bucket policy condition can require the FIPS endpoint, so a deployment sets the variable on every task that syncs the store.

The module supports SC-13, SC-28, SC-8, SC-7, AC-6, and CP-9 through versioning. For AU-2 and AU-12, pass `bucket_arn` to the `cloudtrail` module's `data_event_bucket_arns`, which records every object read and write on the bucket, denied requests included, as a CloudTrail data event within minutes. The server access logs are best-effort and can arrive hours later, so they are not the audit record. The `private-bedrock` root wires both.

## Tests

`tests/rebar_ticket_store.tftest.hcl` plans the module against a mocked provider and asserts on the rendered bucket, key, and role policies, including the denies the policies must not contain. Run it with `inv terraform-test`, which CI also runs.

## Teardown

The bucket is versioned and cannot be deleted while it holds any object version. A principal in `admin_principals` empties it, every version and delete marker included, before `terraform destroy`.

## Usage

```hcl
module "rebar_ticket_store" {
  source = "../rebar-ticket-store"

  create_rebar_ticket_store = true
  name_prefix               = "agentic-platform"
  access_log_bucket         = "agentic-platform-access-logs-123456789012"
  trusted_principals        = [aws_iam_role.activity_worker_task.arn]
  admin_principals          = ["arn:aws:iam::123456789012:role/platform-admin"]
  tags                      = var.tags
}
```
