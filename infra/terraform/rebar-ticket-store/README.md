# rebar-ticket-store

Reusable module that provisions the S3 bucket the rebar ticket store syncs to. rebar keeps its tickets in a git repository and pushes it through the awslabs `git-remote-s3` helper, which stores each ref as git bundles under a key prefix. Nothing is created unless `create_rebar_ticket_store = true`.

## What it provisions

- A bucket named `bucket_name`, or `<name_prefix>-rebar-tickets-<account ID>` when `bucket_name` is empty, with `bucket_prefix` replacing `<name_prefix>-rebar-tickets-` when it is set. The name is the same on every apply, so a destroy and apply cycle finds the same bucket. The bucket has bucket-owner-enforced ownership, a public-access block, versioning, server access logging to `access_log_bucket`, and `prevent_destroy`, so a plan that would destroy or replace it fails.
- SSE-KMS under a dedicated customer managed key with yearly automatic rotation and an S3 Bucket Key. The key policy lets the store roles and `admin_principals` use the key only through S3 and only for this bucket.
- A lifecycle rule that expires replaced bundle versions after `noncurrent_version_retention_days`, 90 by default, and abandoned multipart uploads after 7 days.
- A reader role that lists and reads the prefix, and a writer role that also writes and deletes it, both trusted by `trusted_principals` and `trusted_principals_by_condition`. Set `existing_reader_role_arn` and `existing_writer_role_arn` to use roles the organization issues, and the module creates none.
- A trust for each ARN in `trusted_principals_by_condition` through the account principal and an `aws:PrincipalArn` condition. IAM checks that a principal named in `trusted_principals` exists, so the trust policy depends on that principal's resource, and a destroy of the principal destroys the roles first. The condition carries no such check, so the roles depend on no resource outside the module, and the trust holds when the principal is deleted and recreated under the same name. Each principal so trusted needs an identity policy that allows `sts:AssumeRole` on the roles. The variable rejects `*` and `?`, which `ArnEquals` expands.
- A GitHub Actions OIDC trust on the writer role, through `writer_github_oidc_trust`, so a workflow pushes the store without a stored AWS key. It needs at least one subject, rejects a subject or workflow ref holding `*` because the claims are matched with `StringEquals`, and leaves the reader untouched. Unset, the writer trusts only `trusted_principals`.
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

`inv rebar-store-remote` sets both from the tenant's `tickets.yaml`, and the activity worker refuses to start when the remote does not match that file. Each profile assumes its role. On a workstation that is `source_profile` plus `role_arn`, and in an ECS task it is `credential_source = EcsContainer` plus `role_arn`.

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

`tests/rebar_ticket_store.tftest.hcl` plans the module against a mocked provider and asserts on the rendered bucket, key, and role policies, including the denies the policies must not contain, and on the default and adopted bucket names. `terraform test` cannot assert `prevent_destroy`, because a plan that destroys the bucket fails with an error that `expect_failures` does not match, and the test teardown would fail as well. Check the guard against a deployment with `terraform plan -destroy`, which reports `Instance cannot be destroyed` for the bucket. Run the tests with `inv terraform-test`, which CI also runs.

## Keeping a bucket with a generated name

Earlier versions of the module named the bucket with `bucket_prefix` and a suffix Terraform generated, such as `adp-rebar-tickets-<suffix>`, and a later create generated another name. To keep such a bucket, set `bucket_name` to its current name before applying this version. The provider then plans no change to the bucket, because `bucket` matches the state and `bucket_prefix` is kept from state. Without `bucket_name`, the module derives a different name, the provider plans a replacement, and the plan fails on `prevent_destroy`.

```sh
terraform -chdir=<root> state show '<module address>.aws_s3_bucket.this[0]' | grep -E '^ *bucket +='
```

## Recovery and backup

Versioning keeps every replaced or deleted bundle for `noncurrent_version_retention_days`, so a bad push or a deleted ref is restorable within that window. Versioning does not survive the deletion of the bucket. `prevent_destroy` and the teardown, which keeps the store, keep the bucket from being deleted through Terraform, so versioning and retention cover the failure in which a destroy and apply cycle replaced the bucket and its objects. The module therefore creates no second copy through replication or AWS Backup.

## Teardown

`prevent_destroy` refuses any plan that destroys the bucket, including `terraform destroy` of the module or a change that replaces it. `inv aws-destroy` keeps each ticket store whole with its bucket, key, and roles, unless `--delete-stores` is given. To delete a store, a principal in `admin_principals` empties the bucket, every version and delete marker included, deletes it with `aws s3api delete-bucket`, and removes it from state with `terraform state rm`, after which a destroy removes the rest of the module. `inv aws-destroy --delete-stores` destroys the rest of each store first, which removes the bucket policy that limits object deletes to `admin_principals`, then empties and deletes each bucket and removes it from state. Its principal therefore needs IAM permission for `s3:ListBucketVersions`, `s3:DeleteObjectVersion`, and `s3:DeleteBucket` on the bucket rather than a place in `admin_principals`. A bucket it cannot delete stays in state, and a rerun with the same flags finishes it.

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
