# ecr

Reusable core module that provisions a single ECR repository with scan-on-push, immutable tags, a lifecycle policy that expires untagged images, and optional KMS encryption. It exists so a container registry is managed as shared infrastructure and can be reused or skipped when one already exists.

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

See `variables.tf` and `outputs.tf`. Key inputs are `repository_name`, `image_tag_mutability` (default IMMUTABLE), and `kms_key_arn` (empty uses AES256).
