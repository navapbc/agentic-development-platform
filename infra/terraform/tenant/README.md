# tenant

Reusable module that provisions the AWS resources of one tenant of a shared deployment. A tenant is a trust boundary. Its credentials, config bundle, images, and tickets are reachable only by its own activity worker and its own publisher. Nothing is created unless `create_tenant = true`.

## What it provisions

- A customer managed key, `alias/<name_prefix>-tenant-<tenant>`, with yearly rotation, that encrypts the tenant's secrets and images. Its policy lets the worker decrypt only through Secrets Manager.
- An empty secret for each name in `secrets`, at `<name_prefix>/tenants/<tenant>/<name>` with underscores replaced by hyphens, which the tenant fills with `aws secretsmanager put-secret-value`. Each secret's resource policy denies `GetSecretValue` to every principal except `worker_role_arn`, so an operator or another tenant's worker cannot read it even with a broader IAM policy.
- A repository intelligence token at `<name_prefix>/tenants/<tenant>/repo-intelligence-token`, encrypted with the tenant key, whose value the module generates as 48 letters and digits and writes once through a write-only attribute, so the value never reaches the state. The tenant's worker sends it to repository intelligence, which maps it to the tenant. Its resource policy denies `GetSecretValue` to every principal except `worker_role_arn`, the rotation role of `repo_intelligence_token_rotation`, and `repo_intelligence_reader_arns`, the task role of the repository intelligence service that reads every tenant's current and previous token at runtime. The `tenant` output names it as `repo_intelligence_token_arn` and as `repo_intelligence_token` in `secret_arns`, which the EKS deploy mounts into the worker as `REPO_INTELLIGENCE_TOKEN`. `secrets` may not name `repo_intelligence_token`.
- A rotation of the repository intelligence token every `repo_intelligence_token_rotation_days`, 30 by default and a whole number from 1 to 1000, when `repo_intelligence_token_rotation` names a rotation function and its execution role as `function_arn` and `role_arn`, such as the `token_rotation` output of the [secret-store](../secret-store) module. The rotation sets `rotate_immediately = false`, so the first rotation waits for the schedule. The token's resource policy admits the rotation role to describe, read, put, and restage the secret, and the role's own identity policy grants the tenant key through Secrets Manager. Each rotation stages a new token as `AWSCURRENT` and leaves the version Terraform wrote unchanged, so a rotation causes no plan difference. Null leaves the token unrotated.
- An MCP gateway token at `<name_prefix>/tenants/<tenant>/mcp-gateway-token`, encrypted with the tenant key, whose value the module generates as 48 letters and digits and writes once through a write-only attribute. The tenant's MCP gateway runs as the worker's service account and mounts it as `MCP_GATEWAY_TOKEN`, and no other tenant's gateway accepts it. Its resource policy denies `GetSecretValue` to every principal except `worker_role_arn` and the operators `mcp_gateway_token_reader_arns` names, who give the token to the tenant's MCP clients, so repository intelligence cannot read it. The `tenant` output names it as `mcp_gateway_token_arn` and as `mcp_gateway_token` in `secret_arns`. `secrets` may not name `mcp_gateway_token`.
- A worker policy for `worker_role_arn` that reads the tenant's secrets, its repository intelligence token, and its MCP gateway token, lists and reads `tenants/<tenant>/` of the config store, earlier object versions included, and pulls from the tenant's image prefix. It cannot write the bundle.
- An ECR repository creation template for `<name_prefix>/tenants/<tenant>`, applied on push. A repository the tenant's CI creates by pushing gets immutable tags, the tenant key, the `tenant` tag, and a lifecycle rule that expires untagged images after `untagged_image_expiry_days`. ECR assumes a creation role, which the module creates, to apply the key and the tags.
- With `config_publisher`, a publisher role trusted by the workflows of one GitHub repository on `allowed_refs` through the account's GitHub OIDC provider. It lists, writes, and deletes `tenants/<tenant>/` of the config store, without reading another object, creates and pushes repositories under the tenant's image prefix, and signs with the tenant's signing profile.
- An AWS Signer signing profile on the `Notation-OCI-SHA384-ECDSA` platform, whose ARN the `signing_profile` attribute of the `tenant` output names. Signer never frees the name of a cancelled profile, so the name is generated, prefixed with `<name_prefix>-tenant-<tenant>_` with every hyphen replaced by an underscore. The deployment's image policy trusts this profile only for the tenant's image prefix.
- A rebar ticket store through the [rebar-ticket-store](../rebar-ticket-store) module, in a bucket named `<tenant>-rebar-tickets-<suffix>`, whose reader and writer roles trust the worker and `ticket_store.operators`.
- With `create_webhook_secrets`, the tenant's GitHub and Jira webhook signing secrets at `<name_prefix>/tenants/<tenant>/github-webhook-secret` and `<name_prefix>/tenants/<tenant>/jira-webhook-secret`, encrypted with the tenant key and created empty for an operator to fill with `aws secretsmanager put-secret-value`. Until a secret holds a value, the deployment mounts none and the webhook refuses the tenant's deliveries. Each secret's resource policy denies `GetSecretValue` to every principal except `webhook_reader_arn`, the Pod Identity role of the platform's inbound webhook, so the tenant's worker cannot read them. The module also creates a policy for that role, which the root attaches, that reads the two secrets, decrypts with the tenant key through Secrets Manager, and lists and reads only `tenants/<tenant>/` of the config store, decrypting through S3, so the webhook routes deliveries by the tenant's `repositories.yaml` and `jira.yaml`. The `webhook` output names the secrets as `secret_arns`, keyed `github_webhook_secret` and `jira_webhook_secret`, and the policy as `reader_policy_arn`, and is null without `create_webhook_secrets`. `create_webhook_secrets` needs `webhook_reader_arn`, and `secrets` may not name `github_webhook_secret` or `jira_webhook_secret` when it is set.

Every resource carries a `tenant` tag, which a deployment activates as a cost allocation tag to split the bill by tenant, as [Cost attribution](../examples/private-bedrock/README.md#cost-attribution) describes.

## Names

The tenant's activity worker and its MCP gateway run as the `activity-worker` service account of the `tenant-<tenant>` namespace, and the worker's sandbox, gate, checks, and scanner pods in `tenant-<tenant>-sandbox`. The `tenant` output names both. The deployment binds `worker_role_arn` to that service account through EKS Pod Identity.

## Tests

`tests/tenant.tftest.hcl` plans the module against a mocked provider and asserts that the secrets, the bundle prefix, the image prefix, and the ticket store each admit only the tenant's own principals, that the repository intelligence token admits only the worker, `repo_intelligence_reader_arns`, and the rotation role, that the token rotates only with a rotation function and on the configured whole-day schedule, and that the MCP gateway token admits only the worker and `mcp_gateway_token_reader_arns`. `tests/webhook.tftest.hcl` asserts that the webhook secrets exist only with `create_webhook_secrets`, admit only `webhook_reader_arn`, and stay out of the worker's reach, that the webhook policy reaches only the tenant's webhook secrets and bundle, and the input checks. Run them with `inv terraform-test`.

## Usage

```hcl
module "tenant" {
  source = "../tenant"

  create_tenant     = true
  name_prefix       = "agentic-platform"
  tenant            = "payments"
  description       = "The payments team's FastAPI services."
  worker_role_arn   = module.eks_cluster.pod_identity_role_arns["tenant_payments"]
  secrets           = ["github_token", "jira_api_token"]
  access_log_bucket = "agentic-platform-access-logs-111111111111"

  repo_intelligence_reader_arns    = [module.container_services.repository_intelligence_roles.token_reader_role_arn]
  repo_intelligence_token_rotation = module.secret_store.token_rotation
  config_store = {
    bucket_arn  = module.tenant_config_store.bucket_arn
    kms_key_arn = module.tenant_config_store.kms_key_arn
  }
  config_publisher = {
    oidc_provider_arn = "arn:aws:iam::111111111111:oidc-provider/token.actions.githubusercontent.com"
    github_repository = "example-org/payments-platform-config"
  }
}
```
