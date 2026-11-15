mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "111111111111" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_data "aws_region" {
    defaults = { region = "us-east-1" }
  }
}

override_resource {
  target          = aws_kms_key.this
  override_during = plan
  values          = { arn = "arn:aws:kms:us-east-1:111111111111:key/tenant-adp" }
}

override_resource {
  target          = aws_signer_signing_profile.this
  override_during = plan
  values          = { arn = "arn:aws:signer:us-east-1:111111111111:/signing-profiles/agentic_platform_tenant_adp_abc" }
}

override_resource {
  target          = aws_iam_role.image_creation
  override_during = plan
  values          = { arn = "arn:aws:iam::111111111111:role/agentic-platform-tenant-adp-image-creation" }
}

override_resource {
  target          = aws_secretsmanager_secret.this
  override_during = plan
  values          = { arn = "arn:aws:secretsmanager:us-east-1:111111111111:secret:agentic-platform/tenants/adp/x" }
}

override_resource {
  target          = aws_secretsmanager_secret.repo_intelligence_token
  override_during = plan
  values          = { arn = "arn:aws:secretsmanager:us-east-1:111111111111:secret:agentic-platform/tenants/adp/repo-intelligence-token-a" }
}

override_resource {
  target          = aws_secretsmanager_secret.mcp_gateway_token
  override_during = plan
  values          = { arn = "arn:aws:secretsmanager:us-east-1:111111111111:secret:agentic-platform/tenants/adp/mcp-gateway-token-a" }
}

override_resource {
  target          = aws_secretsmanager_secret.webhook
  override_during = plan
  values          = { arn = "arn:aws:secretsmanager:us-east-1:111111111111:secret:agentic-platform/tenants/adp/webhook-secret-a" }
}

override_resource {
  target          = aws_iam_policy.webhook
  override_during = plan
  values          = { arn = "arn:aws:iam::111111111111:policy/agentic-platform-tenant-adp-webhook-a" }
}

override_resource {
  target          = module.ticket_store.aws_s3_bucket.this
  override_during = plan
  values          = { id = "agentic-platform-tenant-adp-rebar-tickets-abc", arn = "arn:aws:s3:::agentic-platform-tenant-adp-rebar-tickets-abc" }
}

variables {
  name_prefix        = "agentic-platform"
  tenant             = "adp"
  description        = "The platform team's own tickets."
  access_log_bucket  = "agentic-platform-access-logs-111111111111"
  worker_role_arn    = "arn:aws:iam::111111111111:role/agentic-platform-eks-tenant-adp"
  webhook_reader_arn = "arn:aws:iam::111111111111:role/agentic-platform-eks-inbound-webhook"
  secrets            = ["github_token", "jira_api_token"]
  config_store = {
    bucket_arn  = "arn:aws:s3:::agentic-platform-tenant-config-abc"
    kms_key_arn = "arn:aws:kms:us-east-1:111111111111:key/config"
  }
}

run "a_tenant_has_no_webhook_secrets_by_default" {
  command = plan

  variables {
    create_tenant = true
  }

  assert {
    condition     = length(aws_secretsmanager_secret.webhook) == 0 && length(aws_iam_policy.webhook) == 0 && output.webhook == null
    error_message = "A tenant without create_webhook_secrets planned webhook secrets."
  }
}

run "a_disabled_module_creates_no_webhook_secrets" {
  command = plan

  variables {
    create_webhook_secrets = true
  }

  assert {
    condition     = length(aws_secretsmanager_secret.webhook) == 0 && output.webhook == null
    error_message = "The disabled module planned webhook secrets."
  }
}

run "only_the_inbound_webhook_reads_the_tenant_webhook_secrets" {
  command = plan

  variables {
    create_tenant          = true
    create_webhook_secrets = true
  }

  assert {
    condition = (
      aws_secretsmanager_secret.webhook["github_webhook_secret"].name == "agentic-platform/tenants/adp/github-webhook-secret" &&
      aws_secretsmanager_secret.webhook["jira_webhook_secret"].name == "agentic-platform/tenants/adp/jira-webhook-secret" &&
      aws_secretsmanager_secret.webhook["jira_webhook_secret"].kms_key_id == "arn:aws:kms:us-east-1:111111111111:key/tenant-adp" &&
      aws_secretsmanager_secret.webhook["jira_webhook_secret"].tags.tenant == "adp"
    )
    error_message = "A webhook secret is not named under the tenant prefix, encrypted with the tenant key, and tagged."
  }

  assert {
    condition = (
      jsondecode(aws_secretsmanager_secret_policy.webhook["github_webhook_secret"].policy).Statement[0].Principal.AWS == "arn:aws:iam::111111111111:role/agentic-platform-eks-inbound-webhook" &&
      jsondecode(aws_secretsmanager_secret_policy.webhook["github_webhook_secret"].policy).Statement[1].Effect == "Deny" &&
      jsondecode(aws_secretsmanager_secret_policy.webhook["github_webhook_secret"].policy).Statement[1].Condition.ArnNotEquals["aws:PrincipalArn"] == "arn:aws:iam::111111111111:role/agentic-platform-eks-inbound-webhook"
    )
    error_message = "A webhook secret admits a reader other than the inbound webhook."
  }

  assert {
    condition     = !contains(jsondecode(aws_iam_policy.worker[0].policy).Statement[0].Resource, "arn:aws:secretsmanager:us-east-1:111111111111:secret:agentic-platform/tenants/adp/webhook-secret-a")
    error_message = "The tenant worker reads a webhook secret."
  }

  assert {
    condition = (
      output.webhook.secret_arns == {
        github_webhook_secret = "arn:aws:secretsmanager:us-east-1:111111111111:secret:agentic-platform/tenants/adp/webhook-secret-a"
        jira_webhook_secret   = "arn:aws:secretsmanager:us-east-1:111111111111:secret:agentic-platform/tenants/adp/webhook-secret-a"
      } &&
      output.webhook.reader_policy_arn == "arn:aws:iam::111111111111:policy/agentic-platform-tenant-adp-webhook-a"
    )
    error_message = "The webhook output does not name the secrets and the reader policy."
  }
}

run "the_webhook_reads_only_the_tenant_webhook_secrets_and_bundle" {
  command = plan

  variables {
    create_tenant          = true
    create_webhook_secrets = true
  }

  assert {
    condition = (
      jsondecode(aws_iam_policy.webhook[0].policy).Statement[0].Resource == [
        "arn:aws:secretsmanager:us-east-1:111111111111:secret:agentic-platform/tenants/adp/webhook-secret-a",
        "arn:aws:secretsmanager:us-east-1:111111111111:secret:agentic-platform/tenants/adp/webhook-secret-a",
      ] &&
      jsondecode(aws_iam_policy.webhook[0].policy).Statement[1].Resource == "arn:aws:kms:us-east-1:111111111111:key/tenant-adp" &&
      jsondecode(aws_iam_policy.webhook[0].policy).Statement[2].Condition.StringLike["s3:prefix"] == ["tenants/adp/*"] &&
      jsondecode(aws_iam_policy.webhook[0].policy).Statement[3].Resource == "arn:aws:s3:::agentic-platform-tenant-config-abc/tenants/adp/*" &&
      jsondecode(aws_iam_policy.webhook[0].policy).Statement[4].Resource == "arn:aws:kms:us-east-1:111111111111:key/config"
    )
    error_message = "The webhook policy reaches beyond the tenant's webhook secrets and bundle."
  }
}

run "webhook_secrets_need_the_webhook_role" {
  command = plan

  variables {
    create_tenant          = true
    create_webhook_secrets = true
    webhook_reader_arn     = ""
  }

  expect_failures = [terraform_data.webhook_guard]
}

run "a_tenant_secret_may_not_name_a_webhook_secret" {
  command = plan

  variables {
    create_tenant          = true
    create_webhook_secrets = true
    secrets                = ["github_token", "jira_webhook_secret"]
  }

  expect_failures = [terraform_data.webhook_guard]
}
