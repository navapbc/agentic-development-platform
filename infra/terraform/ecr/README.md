# ecr

Reusable core module that provisions a single ECR repository with scan-on-push, immutable tags, a lifecycle policy that expires untagged images, and KMS encryption under a customer-managed key. It exists so a container registry is managed as shared infrastructure and can be reused or skipped when one already exists.

Nothing is created unless `create_repository = true`.

## Usage

```hcl
module "jumphost_ecr" {
  source = "../../ecr"

  create_repository = true
  repository_name   = "agentic-platform/jumphost-base"

  tags = { project = "agentic-dev-platform" }
}
```

To reuse an existing registry instead, set `create_repository = false` and pass the existing repository URL and ARN directly to the consuming module.

## Inputs and outputs

See `variables.tf` and `outputs.tf`. Key inputs are `repository_name` and `kms_key_arn`. An empty `kms_key_arn` creates a module-owned key with rotation enabled. Scan-on-push and immutable tags are fixed and have no input. Changing the encryption key of an existing repository replaces the repository, so push the image again after that apply.
