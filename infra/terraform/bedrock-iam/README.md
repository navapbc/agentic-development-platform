# bedrock-iam

Reusable module that creates the `bedrock:InvokeModel` IAM policy for platform workloads. Attach its output to the role that signs Bedrock inference. It carries no network resources, since private reachability is provided by the `vpc-endpoints` module.

Nothing is created unless `create_invoke_policy = true`.

## Usage

```hcl
module "bedrock_iam" {
  source = "../../bedrock-iam"

  create_invoke_policy = true
  tags                 = { project = "agentic-dev-platform" }
}
```

Attach `module.bedrock_iam.invoke_policy_arn` to the workload role. With the `vpc-endpoints` module providing a `bedrock-runtime` interface endpoint and private DNS on, in-VPC callers reach `bedrock-runtime.<region>.amazonaws.com` over the endpoint, so set the platform's `BEDROCK_CONNECTIVITY_PROVIDER=direct` and leave `BEDROCK_ENDPOINT_URL` empty.

## Inputs and outputs

See `variables.tf` and `outputs.tf`. Key inputs are `model_resources` (defaults to foundation models and inference profiles) and `include_discovery` (default true).
