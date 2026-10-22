# bedrock-jumphost-fargate

Optional, toggleable Terraform module that provisions an ephemeral ECS Fargate jumphost for reaching the private `bedrock-runtime` interface endpoint from a laptop during local testing. It is modeled on the OSRE `tf-jumphost-fargate` module and trimmed to Bedrock port-forwarding only. Nothing is created unless `create_jumphost = true`.

There is no standing host and no SSH. A per-session task runs a minimal image that sleeps for a TTL, and access is ECS Exec over an SSM data channel. The connect script starts a task, waits for it, and opens a port-forward to the Bedrock endpoint.

This module provisions the application resources only. The shared VPC endpoints come from the `vpc-endpoints` module and the container registry comes from the `ecr` module, both passed in as inputs.

## When you do not need this

In a deployed environment the platform runs in-VPC and reaches the endpoint with `BEDROCK_CONNECTIVITY_PROVIDER=direct` and no hop. Use this module only for a laptop path in a test or development account. For a prod-grade laptop path without any host, AWS Client VPN puts the caller inside the VPC and also uses `direct`, at a higher standing cost.

## Usage

```hcl
module "bedrock_jumphost" {
  source = "../../bedrock-jumphost-fargate"

  create_jumphost             = true
  region                      = "us-east-1"
  vpc_id                      = var.vpc_id
  subnet_ids                  = var.private_subnet_ids
  endpoints_security_group_id = module.vpc_endpoints.security_group_id
  image_uri                   = "${module.jumphost_ecr.repository_url}:v1"
  ecr_repository_arn          = module.jumphost_ecr.repository_arn
  access_log_bucket           = var.access_log_bucket

  tags = { project = "agentic-dev-platform" }
}
```

## Image delivery

The task runs the image referenced by `image_uri`, pulled from the `ecr` module's repository. Push it before connecting.

1. `terraform apply` creates the ECR repository (in the `ecr` module) and the jumphost resources.
2. Build and push the image with `inv publish-jumphost-image --tag v1`.
3. Connect with `inv tunnel-start` using the `jumphost` connectivity provider.

To pin a scanned image for production, set `image_uri` to a `repo@sha256` digest.

## Security summary

- Task security group has no ingress. Egress is 443 to the shared endpoint security group only.
- Task role holds only the ECS Exec channel actions and `kms:Decrypt` on the exec key. The execution role holds ECR pull scoped to `ecr_repository_arn`, log write, and secret reads scoped to injected ARNs.
- The ECS Exec data channel is KMS-encrypted and every session is logged to CloudWatch and to a hardened S3 bucket with versioning, encryption, a public-access block, a TLS-only policy, and read access restricted to an allowlist.
- The CloudWatch log group is encrypted with the same customer-managed key, whose policy admits the CloudWatch Logs service for this log group only. The cluster has Container Insights enabled.
- The exec-log bucket sends S3 server access logs to the required `access_log_bucket`.
- Private reachability for the task depends on the `vpc-endpoints` module providing `ssmmessages`, `ecr.api`, `ecr.dkr`, `logs`, `kms`, and the S3 gateway. The `logs` and `kms` endpoints are required because the task ships container logs to CloudWatch and the ECS Exec channel is KMS-encrypted.
- The `secrets` input injects Secrets Manager values by ARN when a future jumphost needs a credential. A Bedrock port-forward needs none, so the default is empty.

## CMS and FedRAMP portability

Set `permissions_boundary_arn` and `iam_path` for a delegated-admin account, set `logs_bucket_kms_key_arn` to a customer-managed key, and point the image build at the approved base registry. None of these change the module shape.

## Inputs and outputs

See `variables.tf` and `outputs.tf`. Key inputs are `create_jumphost` (master off switch), `endpoints_security_group_id`, `attach_endpoints_egress` (default true), `image_uri`, and `ecr_repository_arn`.
