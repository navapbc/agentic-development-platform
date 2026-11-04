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
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
}

override_resource {
  target          = aws_cloudwatch_event_rule.certificate_expiry
  override_during = plan
  values          = { arn = "arn:aws:events:us-east-1:111111111111:rule/agentic-platform-temporal-certificate-expiry" }
}

variables {
  name_prefix = "agentic-platform"
}

run "disabled_module_creates_nothing" {
  command = plan

  assert {
    condition = (
      output.server_repository_name == null && output.admin_tools_repository_name == null &&
      length(aws_secretsmanager_secret.tls) == 0 && length(aws_kms_key.temporal) == 0 &&
      length(aws_cloudwatch_event_rule.certificate_expiry) == 0
    )
    error_message = "The disabled module planned resources."
  }
}

run "repositories_hold_the_server_and_admin_tools_images" {
  command = plan

  variables {
    create_temporal_control_plane = true
  }

  assert {
    condition     = output.server_repository_name == "agentic-platform/temporal-server"
    error_message = "The server repository is not <name_prefix>/temporal-server."
  }

  assert {
    condition     = output.admin_tools_repository_name == "agentic-platform/temporal-admin-tools"
    error_message = "The admin-tools repository is not <name_prefix>/temporal-admin-tools."
  }
}

run "secrets_hold_every_certificate_by_default" {
  command = plan

  variables {
    create_temporal_control_plane = true
  }

  assert {
    condition     = toset(keys(aws_secretsmanager_secret.tls)) == toset(["ca", "internode", "frontend"])
    error_message = "The CA, internode, and frontend secrets are not all created."
  }

  assert {
    condition     = aws_secretsmanager_secret.tls["frontend"].name == "agentic-platform/temporal/tls-frontend"
    error_message = "The frontend secret is not named <name_prefix>/temporal/tls-frontend."
  }

  assert {
    condition     = output.frontend_certificate_source == "secrets"
    error_message = "The frontend source is not secrets without an ACM certificate."
  }

  assert {
    condition     = length(aws_cloudwatch_event_rule.certificate_expiry) == 0 && length(aws_sns_topic.certificate_expiry) == 0
    error_message = "Expiry tracking is planned with no ACM certificate to track."
  }
}

run "an_acm_frontend_removes_the_frontend_secret" {
  command = plan

  variables {
    create_temporal_control_plane = true
    frontend_acm_certificate_arn  = "arn:aws:acm:us-east-1:111111111111:certificate/11111111-2222-3333-4444-555555555555"
  }

  assert {
    condition     = toset(keys(aws_secretsmanager_secret.tls)) == toset(["ca", "internode"])
    error_message = "The frontend secret exists although the NLB presents the ACM certificate."
  }

  assert {
    condition     = output.frontend_certificate_source == "acm"
    error_message = "The frontend source is not acm with an ACM certificate."
  }
}

run "tracked_certificates_publish_their_expiry" {
  command = plan

  variables {
    create_temporal_control_plane = true
    internode_acm_certificate_arn = "arn:aws:acm:us-east-1:111111111111:certificate/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
  }

  assert {
    condition = jsondecode(aws_cloudwatch_event_rule.certificate_expiry[0].event_pattern) == {
      source        = ["aws.acm"]
      "detail-type" = ["ACM Certificate Approaching Expiration", "ACM Certificate Expired"]
      resources     = ["arn:aws:acm:us-east-1:111111111111:certificate/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"]
    }
    error_message = "The expiry rule does not match the tracked certificate's expiry events only."
  }

  assert {
    condition     = toset(keys(aws_secretsmanager_secret.tls)) == toset(["ca", "internode", "frontend"])
    error_message = "Tracking the internode certificate changed which secrets exist."
  }
}

run "an_acm_value_that_is_not_a_certificate_arn_is_refused" {
  command = plan

  variables {
    create_temporal_control_plane = true
    frontend_acm_certificate_arn  = "arn:aws:iam::111111111111:role/not-a-certificate"
  }

  expect_failures = [var.frontend_acm_certificate_arn]
}

run "only_the_listed_readers_may_read_the_secrets" {
  command = plan

  variables {
    create_temporal_control_plane = true
    tls_secret_readers            = ["arn:aws:iam::111111111111:role/temporal-execution"]
  }

  assert {
    condition = jsondecode(local.secret_policies["internode"]).Statement == [
      {
        Sid       = "AllowReaders"
        Effect    = "Allow"
        Principal = { AWS = ["arn:aws:iam::111111111111:role/temporal-execution"] }
        Action    = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
        Resource  = "*"
      },
      {
        Sid       = "DenyOtherReaders"
        Effect    = "Deny"
        Principal = { AWS = "*" }
        Action    = "secretsmanager:GetSecretValue"
        Resource  = "*"
        Condition = { ArnNotEquals = { "aws:PrincipalArn" = ["arn:aws:iam::111111111111:role/temporal-execution"] } }
      },
    ]
    error_message = "The secret policy does not limit reads to the listed readers."
  }
}

run "no_reader_means_nobody_reads" {
  command = plan

  variables {
    create_temporal_control_plane = true
  }

  assert {
    condition     = alltrue([for policy in values(local.secret_policies) : length(jsondecode(policy).Statement) == 1 && !can(jsondecode(policy).Statement[0].Condition)])
    error_message = "The secret policy admits a reader although none is listed."
  }
}
