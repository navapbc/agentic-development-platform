# bootstrap

Composition root that creates the S3 server access-log bucket through the [access-logs](../access-logs) module and the remote state bucket through the [tf-state](../tf-state) module. It runs once per account and region before any other root, and it keeps its own state locally because the bucket it creates cannot hold the state that creates it. That local state contains only the two buckets and their settings, never secret material.

## Apply

Run from this directory with credentials for the target account:

```
terraform init && terraform apply
```

Copy `terraform.tfvars.example` to `terraform.tfvars` to override a default, for example to name the access-log bucket or to point at an existing organization logging bucket with `create_access_log_bucket = false`.

Record the access-log bucket for the composition roots, which require it as `access_log_bucket` in their tfvars:

```
terraform output -raw access_log_bucket
```

Write the emitted backend settings into the composition root that will use the bucket:

```
terraform output -raw backend_hcl > ../examples/private-bedrock/backend.hcl
```

`backend.hcl` is git-ignored because it names a bucket in one account. Each composition root ships a `backend.hcl.example` that shows the shape.

## Moving existing local state

A composition root that was applied before the backend existed holds its state in a local `terraform.tfstate`. After writing `backend.hcl`, migrate that state once from the composition root directory:

```
terraform init -migrate-state -backend-config=backend.hcl
```

Terraform copies the local state into the bucket and then reads and locks it remotely. Confirm with `terraform plan` that no changes are pending, then delete the local `terraform.tfstate` and `terraform.tfstate.backup`.

## Variables

| Name | Default | Purpose |
|---|---|---|
| `region` | `us-east-1` | Region of the state bucket. Use the region of the composition roots it serves. |
| `name_prefix` | `agentic-platform` | Prefix of the bucket name. |
| `kms_key_arn` | empty | SSE-KMS key. Empty uses SSE-S3 AES256. |
| `create_access_log_bucket` | `true` | Create the access-log bucket. Set false to use an existing bucket. |
| `access_log_bucket` | empty | Name of the access-log bucket. Empty derives `<name_prefix>-access-logs-<account id>` for a created bucket. Required when `create_access_log_bucket` is false. |
| `tags` | `{}` | Tags on the created resources. |
