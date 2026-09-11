# vpc-endpoints

Reusable core module that provisions a shared set of VPC endpoints behind one endpoint security group. It consolidates the private connectivity every platform component needs, so an account provisions each endpoint once and application modules consume them rather than creating their own.

Private-only by default: the endpoint security group admits 443 only from the CIDR blocks you pass, `private_dns_enabled` is on, and nothing is created unless `create_endpoints = true`.

## Usage

```hcl
module "vpc_endpoints" {
  source = "../../vpc-endpoints"

  create_endpoints    = true
  region              = "us-east-1"
  vpc_id              = var.vpc_id
  subnet_ids          = var.private_subnet_ids
  allowed_cidr_blocks = var.private_subnet_cidrs

  interface_services = [
    "bedrock-runtime",
    "ssmmessages",
    "ecr.api",
    "ecr.dkr",
    "logs",
    "kms",
  ]

  tags = { project = "agentic-dev-platform" }
}
```

`bedrock-runtime` serves in-VPC Bedrock inference. `ssmmessages` serves the ECS Exec channel. `ecr.api`, `ecr.dkr`, and the S3 gateway serve private image pulls. `logs` and `kms` serve container log delivery and the encrypted exec channel. Include only the services the account does not already have.

## Inputs and outputs

See `variables.tf` and `outputs.tf`. Key toggles are `create_endpoints` (master off switch), `create_s3_gateway` (default true), and `interface_services` (the endpoints to create). The `security_group_id` output is the shared endpoint group that application modules allow their tasks to reach.
