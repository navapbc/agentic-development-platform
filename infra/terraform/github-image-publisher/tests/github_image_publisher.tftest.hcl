mock_provider "aws" {
  mock_resource "aws_iam_openid_connect_provider" {
    defaults = { arn = "arn:aws:iam::111111111111:oidc-provider/token.actions.githubusercontent.com" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::111111111111:role/agentic-platform-github-image-publisher" }
  }
}

variables {
  create_github_image_publisher = true
  oidc_provider_arn             = "arn:aws:iam::111111111111:oidc-provider/token.actions.githubusercontent.com"
  repository_arns = [
    "arn:aws:ecr:us-east-1:111111111111:repository/agentic-platform/platform",
    "arn:aws:ecr:us-east-1:111111111111:repository/agentic-platform/repo-intelligence",
  ]
}

run "disabled_creates_nothing" {
  command = plan

  variables {
    create_github_image_publisher = false
    repository_arns               = []
  }

  assert {
    condition     = length(aws_iam_role.publisher) == 0 && length(aws_iam_openid_connect_provider.github) == 0 && output.role_arn == null
    error_message = "A disabled publisher created a resource."
  }

  assert {
    condition     = length(aws_iam_role.exception) == 0 && output.exception_role_arn == null
    error_message = "A disabled publisher created the image-exception role."
  }
}

run "without_a_config_store_neither_role_reaches_the_exceptions" {
  command = apply

  assert {
    condition = (
      length(aws_iam_role.exception) == 0 &&
      length([for statement in jsondecode(aws_iam_role_policy.push[0].policy).Statement : statement if startswith(statement.Sid, "PruneThe")]) == 0
    )
    error_message = "The module granted exception access without a config store."
  }
}

run "the_publish_role_prunes_the_platform_exceptions_and_reads_the_tenant_ones" {
  command = apply

  variables {
    config_store = {
      bucket_arn  = "arn:aws:s3:::agentic-platform-tenant-config-abc"
      kms_key_arn = "arn:aws:kms:us-east-1:111111111111:key/config"
    }
  }

  assert {
    condition = (
      one([for statement in jsondecode(aws_iam_role_policy.push[0].policy).Statement : statement.Resource if statement.Sid == "PruneThePlatformExceptions"]) == "arn:aws:s3:::agentic-platform-tenant-config-abc/platform/image_exceptions.yaml" &&
      one([for statement in jsondecode(aws_iam_role_policy.push[0].policy).Statement : statement.Action if statement.Sid == "PruneThePlatformExceptions"]) == ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject"] &&
      one([for statement in jsondecode(aws_iam_role_policy.push[0].policy).Statement : statement.Resource if statement.Sid == "ReadTheImagePolicyAndTheTenantExceptions"]) == [
        "arn:aws:s3:::agentic-platform-tenant-config-abc/platform/image_policy.yaml",
        "arn:aws:s3:::agentic-platform-tenant-config-abc/tenants/*/image_exceptions.yaml",
      ] &&
      one([for statement in jsondecode(aws_iam_role_policy.push[0].policy).Statement : statement.Condition.StringEquals["kms:ViaService"] if statement.Sid == "UseTheConfigKeyThroughS3"]) == "s3.us-east-1.amazonaws.com"
    )
    error_message = "The publish role cannot prune the platform exceptions, or reaches beyond the exceptions files."
  }

  assert {
    condition     = jsonencode([for statement in jsondecode(aws_iam_role_policy.push[0].policy).Statement : statement.Action if jsonencode(statement.Resource) == jsonencode("*")]) == jsonencode(["ecr:GetAuthorizationToken", "inspector-scan:ScanSbom"])
    error_message = "The publish role holds a wildcard beyond the ECR token and the SBOM scan."
  }
}

run "the_exception_role_trusts_only_the_image_exception_environment" {
  command = apply

  variables {
    config_store = {
      bucket_arn  = "arn:aws:s3:::agentic-platform-tenant-config-abc"
      kms_key_arn = "arn:aws:kms:us-east-1:111111111111:key/config"
    }
    pull_repository_arns = ["arn:aws:ecr:us-east-1:111111111111:repository/agentic-platform-ecr-public/*"]
  }

  assert {
    condition = (
      jsondecode(aws_iam_role.exception[0].assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:navapbc/agentic-development-platform:environment:image-exception" &&
      jsondecode(aws_iam_role.exception[0].assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:aud"] == "sts.amazonaws.com" &&
      length(keys(jsondecode(aws_iam_role.exception[0].assume_role_policy).Statement[0].Condition)) == 1
    )
    error_message = "The image-exception role trusts a subject other than the image-exception environment."
  }

  assert {
    condition = (
      one([for statement in jsondecode(aws_iam_role_policy.exception[0].policy).Statement : statement.Resource if statement.Sid == "WriteThePlatformExceptions"]) == "arn:aws:s3:::agentic-platform-tenant-config-abc/platform/image_exceptions.yaml" &&
      jsonencode(one([for statement in jsondecode(aws_iam_role_policy.exception[0].policy).Statement : statement.Resource if statement.Sid == "PullThePlatformImages"])) == jsonencode(concat(var.repository_arns, var.pull_repository_arns)) &&
      jsonencode([for statement in jsondecode(aws_iam_role_policy.exception[0].policy).Statement : statement.Action if jsonencode(statement.Resource) == jsonencode("*")]) == jsonencode(["ecr:GetAuthorizationToken", "inspector-scan:ScanSbom"])
    )
    error_message = "The image-exception role writes beyond the platform exceptions file or pulls beyond the given repositories."
  }
}

run "the_role_trusts_only_this_repository_on_main_and_release_tags" {
  command = apply

  assert {
    condition = (
      jsondecode(aws_iam_role.publisher[0].assume_role_policy).Statement[0].Condition.StringLike["token.actions.githubusercontent.com:sub"] ==
      ["repo:navapbc/agentic-development-platform:ref:refs/heads/main", "repo:navapbc/agentic-development-platform:ref:refs/tags/v*"]
    )
    error_message = "The trust policy admits a repository or ref outside main and the v tags."
  }

  assert {
    condition = (
      jsondecode(aws_iam_role.publisher[0].assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:aud"] == "sts.amazonaws.com" &&
      jsondecode(aws_iam_role.publisher[0].assume_role_policy).Statement[0].Principal.Federated == var.oidc_provider_arn
    )
    error_message = "The trust policy does not pin the STS audience or the account's GitHub provider."
  }

  assert {
    condition     = length(aws_iam_openid_connect_provider.github) == 0
    error_message = "The module created a second GitHub provider beside the one it was given."
  }
}

run "immutable_ids_trust_the_immutable_subject_only" {
  command = apply

  variables {
    github_repository_ids = { owner_id = 11320847, repository_id = 1339837798 }
  }

  assert {
    condition = (
      jsondecode(aws_iam_role.publisher[0].assume_role_policy).Statement[0].Condition.StringLike["token.actions.githubusercontent.com:sub"] ==
      [
        "repo:navapbc@11320847/agentic-development-platform@1339837798:ref:refs/heads/main",
        "repo:navapbc@11320847/agentic-development-platform@1339837798:ref:refs/tags/v*",
      ]
    )
    error_message = "The trust policy does not match the immutable subject of the repository."
  }
}

run "the_role_pushes_only_to_the_platform_repositories" {
  command = apply

  assert {
    condition = (
      jsonencode([for statement in jsondecode(aws_iam_role_policy.push[0].policy).Statement : statement.Resource if statement.Sid == "PushThePlatformImages"]) == jsonencode([var.repository_arns]) &&
      jsonencode([for statement in jsondecode(aws_iam_role_policy.push[0].policy).Statement : statement.Action if jsonencode(statement.Resource) == jsonencode("*")]) == jsonencode(["ecr:GetAuthorizationToken", "inspector-scan:ScanSbom"]) &&
      length([for statement in jsondecode(aws_iam_role_policy.push[0].policy).Statement : statement if statement.Sid == "SignThePlatformImages"]) == 0
    )
    error_message = "The role can push outside the platform repositories, holds a wildcard beyond the ECR token and the SBOM scan, or signs without a profile."
  }
}

run "an_empty_provider_arn_creates_the_provider" {
  command = apply

  variables {
    oidc_provider_arn = ""
  }

  assert {
    condition = (
      length(aws_iam_openid_connect_provider.github) == 1 &&
      aws_iam_openid_connect_provider.github[0].url == "https://token.actions.githubusercontent.com" &&
      aws_iam_openid_connect_provider.github[0].client_id_list == toset(["sts.amazonaws.com"])
    )
    error_message = "The module did not create the GitHub provider for the STS audience."
  }
}

run "the_role_needs_repositories_to_push_to" {
  command = plan

  variables {
    repository_arns = []
  }

  expect_failures = [aws_iam_role.publisher]
}

run "a_ref_outside_heads_and_tags_is_refused" {
  command = plan

  variables {
    allowed_refs = ["refs/pull/1/merge"]
  }

  expect_failures = [var.allowed_refs]
}

run "a_signing_profile_lets_the_role_sign_with_it_only" {
  command = apply

  variables {
    signing_profile_arn = "arn:aws:signer:us-east-1:111111111111:/signing-profiles/platform"
  }

  assert {
    condition     = one([for statement in jsondecode(aws_iam_role_policy.push[0].policy).Statement : statement.Resource if statement.Sid == "SignThePlatformImages"]) == var.signing_profile_arn
    error_message = "The role signs with another profile than the platform's."
  }
}
