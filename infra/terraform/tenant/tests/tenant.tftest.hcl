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
  target          = module.ticket_store.aws_s3_bucket.this
  override_during = plan
  values          = { id = "agentic-platform-tenant-adp-rebar-tickets-abc", arn = "arn:aws:s3:::agentic-platform-tenant-adp-rebar-tickets-abc" }
}

variables {
  name_prefix       = "agentic-platform"
  tenant            = "adp"
  description       = "The platform team's own tickets."
  access_log_bucket = "agentic-platform-access-logs-111111111111"
  worker_role_arn   = "arn:aws:iam::111111111111:role/agentic-platform-eks-tenant-adp"
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
      startswith(aws_signer_signing_profile.this[0].name_prefix, "agentic_platform_tenant_adp_")
    )
    error_message = "The publisher trusts another repository, reaches beyond the tenant's prefixes, or signs with another profile."
  }
}

run "the_tenant_owns_a_ticket_store" {
  command = plan

  variables {
    create_tenant = true
  }

  assert {
    condition     = output.tenant.ticket_store.bucket == "agentic-platform-tenant-adp-rebar-tickets-abc" && output.tenant.ticket_store.prefix == "tickets"
    error_message = "The tenant output does not name the tenant's own ticket store."
  }
}
