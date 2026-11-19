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
  target          = aws_secretsmanager_secret.escalation_action_key
  override_during = plan
  values          = { arn = "arn:aws:secretsmanager:us-east-1:111111111111:secret:agentic-platform/tenants/adp/escalation-action-key-a" }
}

override_resource {
  target          = module.ticket_store.aws_s3_bucket.this
  override_during = plan
  values          = { id = "agentic-platform-tenant-adp-rebar-tickets-abc", arn = "arn:aws:s3:::agentic-platform-tenant-adp-rebar-tickets-abc" }
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

variables {
  name_prefix       = "agentic-platform"
  tenant            = "adp"
  description       = "The platform team's own tickets."
  access_log_bucket = "agentic-platform-access-logs-111111111111"
  worker_role_arn   = "arn:aws:iam::111111111111:role/agentic-platform-eks-tenant-adp"
  worker_role_name  = "agentic-platform-eks-tenant-adp"
  secrets           = ["github_token", "jira_api_token"]
  config_store = {
    bucket_arn  = "arn:aws:s3:::agentic-platform-tenant-config-abc"
    kms_key_arn = "arn:aws:kms:us-east-1:111111111111:key/config"
  }
}

run "disabled_module_creates_nothing" {
  command = plan

  assert {
    condition     = length(aws_kms_key.this) == 0 && length(aws_secretsmanager_secret.this) == 0 && output.tenant == null
    error_message = "The disabled module planned resources."
  }
}

run "a_default_tenant_is_refused" {
  command = plan

  variables {
    tenant = "default"
  }

  expect_failures = [var.tenant]
}

run "only_the_tenant_worker_reads_its_secrets" {
  command = plan

  variables {
    create_tenant = true
  }

  assert {
    condition = (
      aws_secretsmanager_secret.this["github_token"].name == "agentic-platform/tenants/adp/github-token" &&
      aws_secretsmanager_secret.this["github_token"].kms_key_id == "arn:aws:kms:us-east-1:111111111111:key/tenant-adp" &&
      aws_secretsmanager_secret.this["github_token"].tags.tenant == "adp"
    )
    error_message = "A tenant secret is not named under the tenant prefix, encrypted with the tenant key, and tagged."
  }

  assert {
    condition = (
      jsondecode(aws_secretsmanager_secret_policy.this["jira_api_token"].policy).Statement[1].Effect == "Deny" &&
      jsondecode(aws_secretsmanager_secret_policy.this["jira_api_token"].policy).Statement[1].Condition.ArnNotEquals["aws:PrincipalArn"] == "arn:aws:iam::111111111111:role/agentic-platform-eks-tenant-adp"
    )
    error_message = "A tenant secret admits a reader other than the tenant worker."
  }
}

run "the_worker_reads_only_its_own_bundle_and_images" {
  command = plan

  variables {
    create_tenant = true
  }

  assert {
    condition = (
      one([for s in jsondecode(aws_iam_policy.worker[0].policy).Statement : s if s.Sid == "ReadTheTenantBundle"]).Resource == "arn:aws:s3:::agentic-platform-tenant-config-abc/tenants/adp/*" &&
      one([for s in jsondecode(aws_iam_policy.worker[0].policy).Statement : s if s.Sid == "ListTheTenantBundle"]).Condition.StringLike["s3:prefix"] == ["tenants/adp/*"] &&
      one([for s in jsondecode(aws_iam_policy.worker[0].policy).Statement : s if s.Sid == "ListTheTenantBundle"]).Action == ["s3:ListBucket", "s3:ListBucketVersions"] &&
      one([for s in jsondecode(aws_iam_policy.worker[0].policy).Statement : s if s.Sid == "PullTheTenantImages"]).Resource == "arn:aws:ecr:us-east-1:111111111111:repository/agentic-platform/tenants/adp/*"
    )
    error_message = "The worker policy reaches beyond the tenant's bundle prefix or image prefix."
  }

  assert {
    condition     = length([for s in jsondecode(aws_iam_policy.worker[0].policy).Statement : s if contains(flatten([s.Action]), "s3:PutObject")]) == 0
    error_message = "The worker may write the tenant's bundle."
  }
}

run "the_image_prefix_creates_encrypted_tagged_repositories_on_push" {
  command = plan

  variables {
    create_tenant = true
  }

  assert {
    condition = (
      aws_ecr_repository_creation_template.this[0].prefix == "agentic-platform/tenants/adp" &&
      aws_ecr_repository_creation_template.this[0].applied_for == toset(["CREATE_ON_PUSH"]) &&
      aws_ecr_repository_creation_template.this[0].image_tag_mutability == "IMMUTABLE" &&
      aws_ecr_repository_creation_template.this[0].encryption_configuration[0].kms_key == "arn:aws:kms:us-east-1:111111111111:key/tenant-adp" &&
      aws_ecr_repository_creation_template.this[0].custom_role_arn == "arn:aws:iam::111111111111:role/agentic-platform-tenant-adp-image-creation" &&
      aws_ecr_repository_creation_template.this[0].resource_tags.tenant == "adp"
    )
    error_message = "The tenant's repositories are not created on push with its key, tags, and immutable tags."
  }
}

run "no_publisher_without_a_repository" {
  command = plan

  variables {
    create_tenant = true
  }

  assert {
    condition     = length(aws_iam_role.publisher) == 0 && output.tenant.publisher_role_arn == null
    error_message = "A publisher role was created with no config_publisher."
  }
}

run "the_publisher_trusts_one_repository_and_writes_one_prefix" {
  command = plan

  variables {
    create_tenant = true
    config_publisher = {
      oidc_provider_arn = "arn:aws:iam::111111111111:oidc-provider/token.actions.githubusercontent.com"
      github_repository = "navapbc/adp-tenant-config"
    }
  }

  assert {
    condition = (
      aws_iam_role.publisher[0].name == "agentic-platform-tenant-adp-publisher" &&
      jsondecode(aws_iam_role.publisher[0].assume_role_policy).Statement[0].Condition.StringLike["token.actions.githubusercontent.com:sub"] == ["repo:navapbc/adp-tenant-config:ref:refs/heads/main"] &&
      one([for s in jsondecode(aws_iam_role_policy.publisher[0].policy).Statement : s if s.Sid == "PublishTheTenantBundle"]).Resource == "arn:aws:s3:::agentic-platform-tenant-config-abc/tenants/adp/*" &&
      one([for s in jsondecode(aws_iam_role_policy.publisher[0].policy).Statement : s if s.Sid == "PushTheTenantImages"]).Resource == "arn:aws:ecr:us-east-1:111111111111:repository/agentic-platform/tenants/adp/*" &&
      one([for s in jsondecode(aws_iam_role_policy.publisher[0].policy).Statement : s if s.Sid == "SignTheTenantImages"]).Resource == "arn:aws:signer:us-east-1:111111111111:/signing-profiles/agentic_platform_tenant_adp_abc" &&
      aws_signer_signing_profile.this[0].name_prefix == "agentic_platform_tenant_adp_"
    )
    error_message = "The publisher trusts another repository, reaches beyond the tenant's prefixes, or signs with another profile."
  }

  assert {
    condition     = aws_iam_role.publisher[0].tags["tenant-config-publisher"] == "adp" && aws_iam_role.publisher[0].tags.tenant == "adp"
    error_message = "The publisher role does not carry the principal tag that admits it to the tenant's config prefix."
  }
}

run "the_publisher_tag_key_follows_the_config_store" {
  command = plan

  variables {
    create_tenant = true
    config_store = {
      bucket_arn        = "arn:aws:s3:::agentic-platform-tenant-config-abc"
      kms_key_arn       = "arn:aws:kms:us-east-1:111111111111:key/config"
      publisher_tag_key = "config-publisher"
    }
    config_publisher = {
      oidc_provider_arn = "arn:aws:iam::111111111111:oidc-provider/token.actions.githubusercontent.com"
      github_repository = "navapbc/adp-tenant-config"
    }
  }

  assert {
    condition     = aws_iam_role.publisher[0].tags["config-publisher"] == "adp" && !contains(keys(aws_iam_role.publisher[0].tags), "tenant-config-publisher")
    error_message = "The publisher role is not tagged with the config store's publisher tag key."
  }
}

run "the_signing_profile_prefix_fits_signer_for_the_longest_tenant_name" {
  command = plan

  variables {
    create_tenant = true
    tenant        = "abcdefghijklmnop"
  }

  assert {
    condition     = can(regex("^[0-9A-Za-z_]{0,38}$", aws_signer_signing_profile.this[0].name_prefix))
    error_message = "The signing profile name_prefix is longer than 38 characters or holds a character Signer refuses."
  }

  assert {
    condition = (
      aws_signer_signing_profile.this[0].name_prefix == "agentic_pla_${substr(sha256("agentic-platform"), 0, 8)}_abcdefghijklmnop_" &&
      length(aws_signer_signing_profile.this[0].name_prefix) == 38
    )
    error_message = "A truncated signing profile prefix does not keep the whole tenant name behind the hashed name_prefix."
  }
}

run "the_signing_profile_prefix_fits_signer_at_the_first_tenant_length_past_the_limit" {
  command = plan

  variables {
    create_tenant = true
    tenant        = "abcdefghijklmn"
  }

  assert {
    condition     = can(regex("^[0-9A-Za-z_]{0,38}$", aws_signer_signing_profile.this[0].name_prefix)) && endswith(aws_signer_signing_profile.this[0].name_prefix, "_abcdefghijklmn_")
    error_message = "The signing profile name_prefix of a 14 character tenant does not fit Signer's limit or loses the tenant name."
  }
}

run "the_signing_profile_prefix_fits_signer_for_a_long_name_prefix" {
  command = plan

  variables {
    create_tenant = true
    name_prefix   = "a-long-deployment-name-prefix1"
    tenant        = "beta"
  }

  assert {
    condition = (
      can(regex("^[0-9A-Za-z_]{0,38}$", aws_signer_signing_profile.this[0].name_prefix)) &&
      endswith(aws_signer_signing_profile.this[0].name_prefix, "_beta_") &&
      startswith(aws_signer_signing_profile.this[0].name_prefix, "a_long_deployment_name_")
    )
    error_message = "The signing profile name_prefix of a long name_prefix does not fit Signer's limit or loses the tenant name."
  }
}

run "the_tenant_owns_a_ticket_store" {
  command = plan

  variables {
    create_tenant = true
  }

  assert {
    condition     = output.tenant.ticket_store.bucket == "agentic-platform-adp-rebar-tickets-111111111111" && output.tenant.ticket_store.prefix == "tickets"
    error_message = "The tenant output does not name the tenant's own ticket store by its deployment, tenant, and account."
  }

  assert {
    condition = contains(jsondecode(aws_iam_policy.worker[0].policy).Statement, {
      Sid      = "AssumeTheTicketStoreRoles"
      Effect   = "Allow"
      Action   = "sts:AssumeRole"
      Resource = ["arn:aws:iam::111111111111:role/agentic-platform-tenant-adp-rebar-tickets-reader", "arn:aws:iam::111111111111:role/agentic-platform-tenant-adp-rebar-tickets-writer"]
    })
    error_message = "The worker policy does not let the worker assume the ticket store roles, which trust it only by condition."
  }
}

run "the_tenant_keeps_an_existing_ticket_store_bucket" {
  command = plan

  variables {
    create_tenant = true
    ticket_store  = { bucket_name = "adp-rebar-tickets-generated-suffix" }
  }

  assert {
    condition     = output.tenant.ticket_store.bucket == "adp-rebar-tickets-generated-suffix"
    error_message = "ticket_store.bucket_name does not name the tenant's ticket store bucket."
  }
}

run "a_worker_role_name_that_differs_from_the_worker_role_is_rejected" {
  command = plan

  variables {
    create_tenant    = true
    worker_role_name = "agentic-platform-eks-tenant-other"
  }

  expect_failures = [terraform_data.inputs_guard]
}

run "the_tenant_worker_and_repository_intelligence_read_its_generated_token" {
  command = plan

  variables {
    create_tenant                 = true
    repo_intelligence_reader_arns = ["arn:aws:iam::111111111111:role/agentic-platform-repo-intel-exec-abc"]
  }

  assert {
    condition = (
      aws_secretsmanager_secret.repo_intelligence_token[0].name == "agentic-platform/tenants/adp/repo-intelligence-token" &&
      aws_secretsmanager_secret.repo_intelligence_token[0].kms_key_id == "arn:aws:kms:us-east-1:111111111111:key/tenant-adp" &&
      aws_secretsmanager_secret.repo_intelligence_token[0].recovery_window_in_days == 30 &&
      aws_secretsmanager_secret.repo_intelligence_token[0].tags.tenant == "adp" &&
      aws_secretsmanager_secret_version.repo_intelligence_token[0].secret_string_wo_version == 1
    )
    error_message = "The token is not a generated secret under the tenant prefix, encrypted with the tenant key, and tagged."
  }

  assert {
    condition = (
      jsondecode(aws_secretsmanager_secret_policy.repo_intelligence_token[0].policy).Statement[1].Effect == "Deny" &&
      jsondecode(aws_secretsmanager_secret_policy.repo_intelligence_token[0].policy).Statement[1].Condition.ArnNotEquals["aws:PrincipalArn"] == [
        "arn:aws:iam::111111111111:role/agentic-platform-eks-tenant-adp",
        "arn:aws:iam::111111111111:role/agentic-platform-repo-intel-exec-abc",
      ]
    )
    error_message = "The token admits a reader other than the tenant worker and repository intelligence."
  }

  assert {
    condition     = contains(one([for s in jsondecode(aws_iam_policy.worker[0].policy).Statement : s if s.Sid == "ReadTheTenantSecrets"]).Resource, "arn:aws:secretsmanager:us-east-1:111111111111:secret:agentic-platform/tenants/adp/repo-intelligence-token-a")
    error_message = "The worker policy does not read the tenant's token."
  }

  assert {
    condition = (
      output.tenant.repo_intelligence_token_arn == "arn:aws:secretsmanager:us-east-1:111111111111:secret:agentic-platform/tenants/adp/repo-intelligence-token-a" &&
      output.tenant.secret_arns.repo_intelligence_token == output.tenant.repo_intelligence_token_arn
    )
    error_message = "The tenant output does not name the token among the secrets the worker mounts."
  }
}

run "the_token_is_unrotated_without_a_rotation_function" {
  command = plan

  variables {
    create_tenant = true
  }

  assert {
    condition     = length(aws_secretsmanager_secret_rotation.repo_intelligence_token) == 0
    error_message = "The token rotates without a rotation function."
  }

  assert {
    condition     = length(jsondecode(aws_secretsmanager_secret_policy.repo_intelligence_token[0].policy).Statement) == 2
    error_message = "The token policy admits a rotation function that is not set."
  }
}

run "the_rotation_function_rotates_the_token_on_a_schedule" {
  command = plan

  variables {
    create_tenant                 = true
    repo_intelligence_reader_arns = ["arn:aws:iam::111111111111:role/agentic-platform-repo-intel-task-abc"]
    repo_intelligence_token_rotation = {
      function_arn = "arn:aws:lambda:us-east-1:111111111111:function:agentic-platform-token-rotation"
      role_arn     = "arn:aws:iam::111111111111:role/agentic-platform-token-rotation-abc"
    }
  }

  assert {
    condition = (
      aws_secretsmanager_secret_rotation.repo_intelligence_token[0].rotation_lambda_arn == "arn:aws:lambda:us-east-1:111111111111:function:agentic-platform-token-rotation" &&
      aws_secretsmanager_secret_rotation.repo_intelligence_token[0].rotation_rules[0].automatically_after_days == 30 &&
      aws_secretsmanager_secret_rotation.repo_intelligence_token[0].rotate_immediately == false
    )
    error_message = "The token does not rotate every 30 days through the rotation function."
  }

  assert {
    condition = (
      jsondecode(aws_secretsmanager_secret_policy.repo_intelligence_token[0].policy).Statement[1].Condition.ArnNotEquals["aws:PrincipalArn"] == [
        "arn:aws:iam::111111111111:role/agentic-platform-eks-tenant-adp",
        "arn:aws:iam::111111111111:role/agentic-platform-repo-intel-task-abc",
        "arn:aws:iam::111111111111:role/agentic-platform-token-rotation-abc",
      ] &&
      jsondecode(aws_secretsmanager_secret_policy.repo_intelligence_token[0].policy).Statement[0].Principal.AWS == [
        "arn:aws:iam::111111111111:role/agentic-platform-eks-tenant-adp",
        "arn:aws:iam::111111111111:role/agentic-platform-repo-intel-task-abc",
      ]
    )
    error_message = "The rotation function is refused the token, or reads it as a consumer."
  }

  assert {
    condition = (
      jsondecode(aws_secretsmanager_secret_policy.repo_intelligence_token[0].policy).Statement[2].Principal.AWS == "arn:aws:iam::111111111111:role/agentic-platform-token-rotation-abc" &&
      contains(jsondecode(aws_secretsmanager_secret_policy.repo_intelligence_token[0].policy).Statement[2].Action, "secretsmanager:PutSecretValue") &&
      contains(jsondecode(aws_secretsmanager_secret_policy.repo_intelligence_token[0].policy).Statement[2].Action, "secretsmanager:UpdateSecretVersionStage")
    )
    error_message = "The token policy does not let the rotation function stage a new token."
  }
}

run "the_rotation_schedule_is_configurable" {
  command = plan

  variables {
    create_tenant                         = true
    repo_intelligence_token_rotation_days = 7
    repo_intelligence_token_rotation = {
      function_arn = "arn:aws:lambda:us-east-1:111111111111:function:agentic-platform-token-rotation"
      role_arn     = "arn:aws:iam::111111111111:role/agentic-platform-token-rotation-abc"
    }
  }

  assert {
    condition     = aws_secretsmanager_secret_rotation.repo_intelligence_token[0].rotation_rules[0].automatically_after_days == 7
    error_message = "The rotation schedule ignores repo_intelligence_token_rotation_days."
  }
}

run "a_rotation_schedule_is_whole_days" {
  command = plan

  variables {
    repo_intelligence_token_rotation_days = 0
  }

  expect_failures = [var.repo_intelligence_token_rotation_days]
}

run "a_tenant_secret_may_not_name_the_generated_token" {
  command = plan

  variables {
    secrets = ["github_token", "repo_intelligence_token"]
  }

  expect_failures = [var.secrets]
}

run "only_the_tenant_worker_identity_reads_its_gateway_token" {
  command = plan

  variables {
    create_tenant                 = true
    repo_intelligence_reader_arns = ["arn:aws:iam::111111111111:role/agentic-platform-repo-intel-exec-abc"]
  }

  assert {
    condition = (
      aws_secretsmanager_secret.mcp_gateway_token[0].name == "agentic-platform/tenants/adp/mcp-gateway-token" &&
      aws_secretsmanager_secret.mcp_gateway_token[0].kms_key_id == "arn:aws:kms:us-east-1:111111111111:key/tenant-adp" &&
      aws_secretsmanager_secret.mcp_gateway_token[0].recovery_window_in_days == 30 &&
      aws_secretsmanager_secret.mcp_gateway_token[0].tags.tenant == "adp" &&
      aws_secretsmanager_secret_version.mcp_gateway_token[0].secret_string_wo_version == 1
    )
    error_message = "The gateway token is not a generated secret under the tenant prefix, encrypted with the tenant key, and tagged."
  }

  assert {
    condition = (
      jsondecode(aws_secretsmanager_secret_policy.mcp_gateway_token[0].policy).Statement[0].Principal.AWS == ["arn:aws:iam::111111111111:role/agentic-platform-eks-tenant-adp"] &&
      jsondecode(aws_secretsmanager_secret_policy.mcp_gateway_token[0].policy).Statement[1].Effect == "Deny" &&
      jsondecode(aws_secretsmanager_secret_policy.mcp_gateway_token[0].policy).Statement[1].Condition.ArnNotEquals["aws:PrincipalArn"] == ["arn:aws:iam::111111111111:role/agentic-platform-eks-tenant-adp"]
    )
    error_message = "The gateway token admits a reader other than the tenant worker identity, such as repository intelligence."
  }

  assert {
    condition     = contains(one([for s in jsondecode(aws_iam_policy.worker[0].policy).Statement : s if s.Sid == "ReadTheTenantSecrets"]).Resource, "arn:aws:secretsmanager:us-east-1:111111111111:secret:agentic-platform/tenants/adp/mcp-gateway-token-a")
    error_message = "The worker policy does not read the tenant's gateway token."
  }

  assert {
    condition = (
      output.tenant.mcp_gateway_token_arn == "arn:aws:secretsmanager:us-east-1:111111111111:secret:agentic-platform/tenants/adp/mcp-gateway-token-a" &&
      output.tenant.secret_arns.mcp_gateway_token == output.tenant.mcp_gateway_token_arn
    )
    error_message = "The tenant output does not name the gateway token among the secrets the tenant's pods mount."
  }
}

run "the_gateway_token_admits_the_named_operators" {
  command = plan

  variables {
    create_tenant                 = true
    mcp_gateway_token_reader_arns = ["arn:aws:iam::111111111111:role/adp-operator"]
  }

  assert {
    condition = (
      jsondecode(aws_secretsmanager_secret_policy.mcp_gateway_token[0].policy).Statement[0].Principal.AWS == ["arn:aws:iam::111111111111:role/agentic-platform-eks-tenant-adp", "arn:aws:iam::111111111111:role/adp-operator"] &&
      jsondecode(aws_secretsmanager_secret_policy.mcp_gateway_token[0].policy).Statement[1].Condition.ArnNotEquals["aws:PrincipalArn"] == ["arn:aws:iam::111111111111:role/agentic-platform-eks-tenant-adp", "arn:aws:iam::111111111111:role/adp-operator"]
    )
    error_message = "The gateway token does not admit exactly the tenant worker and the named operators."
  }
}

run "a_tenant_secret_may_not_name_the_gateway_token" {
  command = plan

  variables {
    secrets = ["github_token", "mcp_gateway_token"]
  }

  expect_failures = [var.secrets]
}

run "disabled_module_creates_no_gateway_token" {
  command = plan

  assert {
    condition     = length(aws_secretsmanager_secret.mcp_gateway_token) == 0 && output.tenant == null
    error_message = "A disabled tenant module creates a gateway token."
  }
}
