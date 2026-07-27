# bedrock-vpce

Optional, toggleable Terraform module that provisions a project's own `bedrock-runtime` interface VPC endpoint plus an IAM `bedrock:InvokeModel` policy. It exists so a project with no existing Bedrock endpoint can satisfy the platform's reachability contract (R7) without depending on OSRE-specific plumbing.

Private-only by default: the endpoint admits 443 only from the CIDR blocks you pass, `private_dns_enabled` is on, and nothing is created unless `create_bedrock_vpce = true`.

## Usage

```hcl
module "bedrock_vpce" {
  source = "../../infra/terraform/bedrock-vpce"

  create_bedrock_vpce = true
  region              = "us-east-1"
  vpc_id              = var.vpc_id
  subnet_ids          = var.private_subnet_ids
  allowed_cidr_blocks = var.app_subnet_cidrs

  tags = { project = "agentic-dev-platform" }
}
```

Attach `module.bedrock_vpce.invoke_policy_arn` to the workload role. With private DNS enabled, in-VPC callers reach `bedrock-runtime.<region>.amazonaws.com` over the endpoint, so set the platform's `BEDROCK_CONNECTIVITY_PROVIDER=direct` and leave `BEDROCK_ENDPOINT_URL` empty.

## Inputs

See `variables.tf`. Key toggles: `create_bedrock_vpce` (master off switch), `private_dns_enabled` (default true), `create_invoke_policy` (default true).
