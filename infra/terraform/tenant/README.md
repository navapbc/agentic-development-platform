# tenant

Reusable module that provisions the AWS resources of one tenant of a shared deployment. A tenant is a trust boundary. Its credentials, config bundle, images, and tickets are reachable only by its own activity worker and its own publisher. Nothing is created unless `create_tenant = true`.

## What it provisions

- A customer managed key, `alias/<name_prefix>-tenant-<tenant>`, with yearly rotation, that encrypts the tenant's secrets and images. Its policy lets the worker decrypt only through Secrets Manager.
- An empty secret for each name in `secrets`, at `<name_prefix>/tenants/<tenant>/<name>` with underscores replaced by hyphens, which the tenant fills with `aws secretsmanager put-secret-value`. Each secret's resource policy denies `GetSecretValue` to every principal except `worker_role_arn`, so an operator or another tenant's worker cannot read it even with a broader IAM policy.
- A worker policy for `worker_role_arn` that reads the tenant's secrets, lists and reads `tenants/<tenant>/` of the config store, and pulls from the tenant's image prefix. It cannot write the bundle.
- An ECR repository creation template for `<name_prefix>/tenants/<tenant>`, applied on push. A repository the tenant's CI creates by pushing gets immutable tags, the tenant key, the `tenant` tag, and a lifecycle rule that expires untagged images after `untagged_image_expiry_days`. ECR assumes a creation role, which the module creates, to apply the key and the tags.
- With `config_publisher`, a publisher role trusted by the workflows of one GitHub repository on `allowed_refs` through the account's GitHub OIDC provider. It lists, writes, and deletes `tenants/<tenant>/` of the config store, without reading another object, and creates and pushes repositories under the tenant's image prefix.
- A rebar ticket store through the [rebar-ticket-store](../rebar-ticket-store) module, in a bucket named `<tenant>-rebar-tickets-<suffix>`, whose reader and writer roles trust the worker and `ticket_store.operators`.

Every resource carries a `tenant` tag, which a deployment activates as a cost allocation tag to split the bill by tenant.

## Names

The tenant's activity worker runs as the `activity-worker` service account of the `tenant-<tenant>` namespace, and its sandbox, gate, checks, and scanner pods in `tenant-<tenant>-sandbox`. The `tenant` output names both. The deployment binds `worker_role_arn` to that service account through EKS Pod Identity.

## Tests

`tests/tenant.tftest.hcl` plans the module against a mocked provider and asserts that the secrets, the bundle prefix, the image prefix, and the ticket store each admit only the tenant's own principals. Run it with `inv terraform-test`.

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
