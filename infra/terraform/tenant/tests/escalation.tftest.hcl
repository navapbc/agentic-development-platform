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
  target          = module.ticket_store.aws_iam_role.reader
  override_during = plan
  values          = { arn = "arn:aws:iam::111111111111:role/agentic-platform-tenant-adp-rebar-tickets-reader" }
}

override_resource {
  target          = module.ticket_store.aws_iam_role.writer
  override_during = plan
  values          = { arn = "arn:aws:iam::111111111111:role/agentic-platform-tenant-adp-rebar-tickets-writer" }
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
  target          = aws_secretsmanager_secret.escalation_action_key
  override_during = plan
  values          = { arn = "arn:aws:secretsmanager:us-east-1:111111111111:secret:agentic-platform/tenants/adp/escalation-action-key-a" }
}

override_resource {
  target          = aws_secretsmanager_secret.escalation_action_key_sha256
  override_during = plan
  values          = { arn = "arn:aws:secretsmanager:us-east-1:111111111111:secret:agentic-platform/tenants/adp/escalation-action-key-sha256-a" }
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
  worker_role_name   = "agentic-platform-eks-tenant-adp"
  review_role_arn    = "arn:aws:iam::111111111111:role/agentic-platform-eks-tenant-adp-review"
  webhook_reader_arn = "arn:aws:iam::111111111111:role/agentic-platform-eks-inbound-webhook"
  secrets            = ["github_token", "jira_api_token"]
  config_store = {
    bucket_arn  = "arn:aws:s3:::agentic-platform-tenant-config-abc"
    kms_key_arn = "arn:aws:kms:us-east-1:111111111111:key/config"
  }
}

run "only_the_tenant_worker_and_the_inbound_webhook_read_the_escalation_action_key" {
  command = plan

  variables {
    create_tenant          = true
    create_webhook_secrets = true
  }

  assert {
    condition = (
      aws_secretsmanager_secret.escalation_action_key[0].name == "agentic-platform/tenants/adp/escalation-action-key" &&
      aws_secretsmanager_secret.escalation_action_key[0].kms_key_id == "arn:aws:kms:us-east-1:111111111111:key/tenant-adp" &&
      aws_secretsmanager_secret.escalation_action_key[0].tags.tenant == "adp"
    )
    error_message = "The escalation action key is not named under the tenant prefix, encrypted with the tenant key, and tagged."
  }

  assert {
    condition = (
      jsondecode(aws_secretsmanager_secret_policy.escalation_action_key[0].policy).Statement[0].Principal.AWS == [
        "arn:aws:iam::111111111111:role/agentic-platform-eks-tenant-adp",
        "arn:aws:iam::111111111111:role/agentic-platform-eks-inbound-webhook",
      ] &&
      jsondecode(aws_secretsmanager_secret_policy.escalation_action_key[0].policy).Statement[1].Effect == "Deny" &&
      jsondecode(aws_secretsmanager_secret_policy.escalation_action_key[0].policy).Statement[1].Condition.ArnNotEquals["aws:PrincipalArn"] == [
        "arn:aws:iam::111111111111:role/agentic-platform-eks-tenant-adp",
        "arn:aws:iam::111111111111:role/agentic-platform-eks-inbound-webhook",
      ] &&
      length(jsondecode(aws_secretsmanager_secret_policy.escalation_action_key[0].policy).Statement) == 2
    )
    error_message = "The escalation action key admits a reader other than the tenant worker and the inbound webhook."
  }

  assert {
    condition     = contains(one([for s in jsondecode(aws_iam_policy.worker[0].policy).Statement : s if s.Sid == "ReadTheTenantSecrets"]).Resource, "arn:aws:secretsmanager:us-east-1:111111111111:secret:agentic-platform/tenants/adp/escalation-action-key-a")
    error_message = "The worker policy does not read the tenant's escalation action key."
  }

  assert {
    condition = (
      output.tenant.secret_arns.escalation_action_key == "arn:aws:secretsmanager:us-east-1:111111111111:secret:agentic-platform/tenants/adp/escalation-action-key-a" &&
      output.webhook.secret_arns.escalation_action_key == output.tenant.secret_arns.escalation_action_key
    )
    error_message = "The tenant and webhook outputs do not name the escalation action key the worker and the inbound webhook mount."
  }
}

run "a_single_tenant_deployment_admits_only_the_worker_to_the_escalation_action_key" {
  command = plan

  variables {
    create_tenant = true
  }

  assert {
    condition = (
      jsondecode(aws_secretsmanager_secret_policy.escalation_action_key[0].policy).Statement[1].Condition.ArnNotEquals["aws:PrincipalArn"] == [
        "arn:aws:iam::111111111111:role/agentic-platform-eks-tenant-adp",
      ] &&
      output.tenant.secret_arns.escalation_action_key == "arn:aws:secretsmanager:us-east-1:111111111111:secret:agentic-platform/tenants/adp/escalation-action-key-a"
    )
    error_message = "Without create_webhook_secrets the escalation action key admits a reader other than the tenant worker."
  }
}

run "a_single_tenant_slack_webhook_reads_only_the_slack_signing_secret_and_the_action_key" {
  command = plan

  variables {
    create_tenant               = true
    create_slack_webhook_secret = true
  }

  assert {
    condition     = keys(aws_secretsmanager_secret.webhook) == ["slack_signing_secret"]
    error_message = "A single tenant's Slack webhook planned a GitHub or Jira webhook secret."
  }

  assert {
    condition = (
      jsondecode(aws_secretsmanager_secret_policy.escalation_action_key[0].policy).Statement[1].Condition.ArnNotEquals["aws:PrincipalArn"] == [
        "arn:aws:iam::111111111111:role/agentic-platform-eks-tenant-adp",
        "arn:aws:iam::111111111111:role/agentic-platform-eks-inbound-webhook",
      ]
    )
    error_message = "A single tenant's Slack webhook does not admit the inbound webhook to the action key beside the worker."
  }

  assert {
    condition     = toset(keys(output.webhook.secret_arns)) == toset(["escalation_action_key", "slack_signing_secret"])
    error_message = "A single tenant's webhook output names a secret other than the Slack signing secret and the action key."
  }
}

run "a_slack_webhook_secret_needs_the_webhook_role" {
  command = plan

  variables {
    create_tenant               = true
    create_slack_webhook_secret = true
    webhook_reader_arn          = ""
  }

  expect_failures = [terraform_data.webhook_guard]
}

run "a_disabled_module_creates_no_escalation_action_key" {
  command = plan

  assert {
    condition     = length(aws_secretsmanager_secret.escalation_action_key) == 0 && output.tenant == null
    error_message = "The disabled module planned an escalation action key."
  }
}

run "a_tenant_secret_may_not_name_the_escalation_action_key" {
  command = plan

  variables {
    secrets = ["github_token", "escalation_action_key"]
  }

  expect_failures = [var.secrets]
}

run "only_the_tenant_worker_and_the_slack_posting_service_read_the_action_key_fingerprint" {
  command = plan

  variables {
    create_tenant           = true
    slack_poster_reader_arn = "arn:aws:iam::111111111111:role/agentic-platform-eks-slack-poster"
  }

  assert {
    condition = (
      aws_secretsmanager_secret.escalation_action_key_sha256[0].name == "agentic-platform/tenants/adp/escalation-action-key-sha256" &&
      aws_secretsmanager_secret.escalation_action_key_sha256[0].kms_key_id == "arn:aws:kms:us-east-1:111111111111:key/tenant-adp"
    )
    error_message = "The action key fingerprint is not named under the tenant prefix and encrypted with the tenant key."
  }

  assert {
    condition = (
      jsondecode(aws_secretsmanager_secret_policy.escalation_action_key_sha256[0].policy).Statement[0].Principal.AWS == [
        "arn:aws:iam::111111111111:role/agentic-platform-eks-tenant-adp",
        "arn:aws:iam::111111111111:role/agentic-platform-eks-slack-poster",
      ] &&
      jsondecode(aws_secretsmanager_secret_policy.escalation_action_key_sha256[0].policy).Statement[1].Condition.ArnNotEquals["aws:PrincipalArn"] == [
        "arn:aws:iam::111111111111:role/agentic-platform-eks-tenant-adp",
        "arn:aws:iam::111111111111:role/agentic-platform-eks-slack-poster",
      ] &&
      !contains(jsondecode(aws_secretsmanager_secret_policy.escalation_action_key[0].policy).Statement[0].Principal.AWS, "arn:aws:iam::111111111111:role/agentic-platform-eks-slack-poster")
    )
    error_message = "The fingerprint admits a reader other than the tenant worker and the Slack posting service, or the posting service reads the key itself."
  }

  assert {
    condition     = output.tenant.action_key_fingerprint_arn == "arn:aws:secretsmanager:us-east-1:111111111111:secret:agentic-platform/tenants/adp/escalation-action-key-sha256-a"
    error_message = "The tenant output does not name the action key fingerprint."
  }
}

run "the_key_and_its_fingerprint_are_written_at_one_version" {
  command = plan

  variables {
    create_tenant                 = true
    escalation_action_key_version = 2
  }

  assert {
    condition = (
      aws_secretsmanager_secret_version.escalation_action_key[0].secret_string_wo_version == 2 &&
      aws_secretsmanager_secret_version.escalation_action_key_sha256[0].secret_string_wo_version == 2
    )
    error_message = "The action key and its fingerprint are not written at the same version."
  }

  assert {
    condition     = jsondecode(aws_secretsmanager_secret_policy.escalation_action_key_sha256[0].policy).Statement[0].Principal.AWS == ["arn:aws:iam::111111111111:role/agentic-platform-eks-tenant-adp"]
    error_message = "Without the Slack posting service the fingerprint admits a reader other than the tenant worker."
  }
}

run "the_action_key_version_is_a_whole_number_of_at_least_one" {
  command = plan

  variables {
    create_tenant                 = true
    escalation_action_key_version = 1.5
  }

  expect_failures = [var.escalation_action_key_version]
}

