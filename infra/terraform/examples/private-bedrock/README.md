# private-bedrock

Reference composition root that stands up the private Bedrock path and the Fargate jumphost for a single account. It wires the core infrastructure modules (`vpc-endpoints`, `ecr`, `cloudtrail`) and the application modules (`bedrock-iam`, `bedrock-jumphost-fargate`) into one apply.

It works from zero or against existing infrastructure. Set `create_network = true` to provision a private VPC and subnets, or set it false and supply `vpc_id`, `subnet_ids`, and `endpoint_allowed_cidrs`. Each core piece has its own toggle, so an account that already has VPC endpoints, a registry, or an organization CloudTrail can skip them.

## Usage

Copy an example tfvars file, fill in the placeholders, then apply under the platform profile.

Greenfield, create everything:

```
cp greenfield.tfvars.example greenfield.tfvars
terraform init
AWS_PROFILE=agentic-platform terraform apply -var-file=greenfield.tfvars
```

Existing network, reuse a VPC and subnets:

```
cp existing-network.tfvars.example existing-network.tfvars
terraform init
AWS_PROFILE=agentic-platform terraform apply -var-file=existing-network.tfvars
```

## After apply

1. Push the jumphost image to the repository shown in the `jumphost_ecr_repository_url` output with `inv publish-jumphost-image --tag v1`.
2. Attach the `bedrock_invoke_policy_arn` output to the workload role that signs Bedrock inference.
3. Bring up the platform and open the tunnel with `inv tunnel-start --detach`.

## Toggles for existing infrastructure

| Variable | Set false when |
|----------|----------------|
| `create_network` | A VPC and private subnets already exist. Then set `vpc_id`, `subnet_ids`, and `endpoint_allowed_cidrs`. |
| `create_endpoints` | The account already has the bedrock-runtime, ssmmessages, ECR, logs, and kms endpoints. |
| `create_ecr_repository` | A registry already holds the jumphost image. Then set `jumphost_repository_name`. |
| `create_cloudtrail` | An organization CloudTrail already covers this account. |
