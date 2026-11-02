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
  target          = aws_s3_bucket.this
  override_during = plan
  values = {
    id  = "agentic-platform-rebar-tickets-abc"
    arn = "arn:aws:s3:::agentic-platform-rebar-tickets-abc"
  }
}

override_resource {
  target          = aws_kms_key.this
  override_during = plan
  values          = { arn = "arn:aws:kms:us-east-1:111111111111:key/store" }
}

override_resource {
  target          = aws_iam_role.reader
  override_during = plan
  values          = { arn = "arn:aws:iam::111111111111:role/agentic-platform-rebar-tickets-reader" }
}

override_resource {
  target          = aws_iam_role.writer
  override_during = plan
  values          = { arn = "arn:aws:iam::111111111111:role/agentic-platform-rebar-tickets-writer" }
}

variables {
  name_prefix       = "agentic-platform"
  access_log_bucket = "agentic-platform-access-logs-111111111111"
}

run "disabled_module_creates_nothing" {
  command = plan

  assert {
    condition     = length(aws_s3_bucket.this) == 0 && length(aws_kms_key.this) == 0 && length(aws_iam_role.reader) == 0
    error_message = "The disabled module planned resources."
  }
}

run "bucket_policy_denies_what_the_store_forbids" {
  command = plan

  variables {
    create_rebar_ticket_store = true
    trusted_principals        = ["arn:aws:iam::111111111111:role/activity-worker"]
    admin_principals          = ["arn:aws:iam::111111111111:role/platform-admin"]
  }

  assert {
    condition     = alltrue([for s in jsondecode(local.bucket_policy).Statement : s.Effect == "Deny"])
    error_message = "The bucket policy grants access instead of only denying it."
  }

  assert {
    condition = jsondecode(local.bucket_policy).Statement[2].Condition.ArnNotLike["aws:PrincipalArn"] == [
      "arn:aws:iam::111111111111:role/agentic-platform-rebar-tickets-reader",
      "arn:aws:iam::111111111111:role/agentic-platform-rebar-tickets-writer",
      "arn:aws:iam::111111111111:role/platform-admin",
    ]
    error_message = "The object allowlist is not the reader, the writer, and the admins."
  }

  assert {
    condition = alltrue([
      for s in jsondecode(local.bucket_policy).Statement :
      !contains(flatten([s.Action]), "s3:ListBucket") && !contains(flatten([s.Action]), "s3:ListBucket*")
    ])
    error_message = "The bucket policy denies s3:ListBucket, which makes the provider drop the bucket from state."
  }

  assert {
    condition = alltrue([
      for s in jsondecode(local.bucket_policy).Statement :
      try(s.Condition.Null, null) != null
      if can(s.Condition.StringNotEquals)
    ])
    error_message = "An encryption deny lacks its Null check, so it denies uploads without an encryption header."
  }

  assert {
    condition = alltrue([
      for s in jsondecode(local.bucket_policy).Statement :
      !can(s.Condition.StringNotEqualsIfExists)
    ])
    error_message = "A deny uses an IfExists operator, which matches when the header is absent."
  }

  assert {
    condition     = jsondecode(local.bucket_policy).Statement[3].Condition.ArnLike["aws:PrincipalArn"] == "arn:aws:iam::111111111111:role/agentic-platform-rebar-tickets-reader"
    error_message = "The reader write deny does not name the reader."
  }

  assert {
    condition     = aws_s3_bucket_policy.this[0].policy == local.bucket_policy
    error_message = "The bucket policy resource does not carry the rendered policy."
  }
}

run "key_policy_limits_use_to_s3_for_this_bucket" {
  command = plan

  variables {
    create_rebar_ticket_store = true
    trusted_principals        = ["arn:aws:iam::111111111111:role/activity-worker"]
  }

  assert {
    condition = alltrue([
      for s in slice(jsondecode(local.key_policy).Statement, 1, 3) :
      s.Condition.StringEquals["kms:ViaService"] == "s3.us-east-1.amazonaws.com"
      && s.Condition.StringLike["kms:EncryptionContext:aws:s3:arn"] == [
        "arn:aws:s3:::agentic-platform-rebar-tickets-abc",
        "arn:aws:s3:::agentic-platform-rebar-tickets-abc/*",
      ]
    ])
    error_message = "Key use is not limited to S3 for this bucket."
  }

  assert {
    condition     = jsondecode(local.key_policy).Statement[2].Principal.AWS == ["arn:aws:iam::111111111111:role/agentic-platform-rebar-tickets-writer"]
    error_message = "A principal other than the writer may generate data keys."
  }
}

run "role_policies_stay_within_the_prefix" {
  command = plan

  variables {
    create_rebar_ticket_store = true
    trusted_principals        = ["arn:aws:iam::111111111111:role/activity-worker"]
  }

  assert {
    condition     = jsondecode(local.reader_policy).Statement[1].Action == "s3:GetObject" && jsondecode(local.reader_policy).Statement[1].Resource == "arn:aws:s3:::agentic-platform-rebar-tickets-abc/tickets/*"
    error_message = "The reader can do more than read the store prefix."
  }

  assert {
    condition     = jsondecode(local.writer_policy).Statement[1].Resource == "arn:aws:s3:::agentic-platform-rebar-tickets-abc/tickets/*"
    error_message = "The writer can write outside the store prefix."
  }

  assert {
    condition     = jsondecode(local.assume_policy).Statement[0].Principal.AWS == ["arn:aws:iam::111111111111:role/activity-worker"]
    error_message = "The roles trust a principal other than trusted_principals."
  }
}

run "supplied_roles_replace_the_created_roles" {
  command = plan

  variables {
    create_rebar_ticket_store = true
    existing_reader_role_arn  = "arn:aws:iam::111111111111:role/org-reader"
    existing_writer_role_arn  = "arn:aws:iam::111111111111:role/org-writer"
  }

  assert {
    condition     = length(aws_iam_role.reader) == 0 && output.reader_role_arn == "arn:aws:iam::111111111111:role/org-reader"
    error_message = "The module created roles although existing roles were supplied."
  }
}

run "one_supplied_role_is_rejected" {
  command = plan

  variables {
    create_rebar_ticket_store = true
    existing_reader_role_arn  = "arn:aws:iam::111111111111:role/org-reader"
  }

  expect_failures = [terraform_data.inputs_guard]
}

run "the_same_supplied_role_twice_is_rejected" {
  command = plan

  variables {
    create_rebar_ticket_store = true
    existing_reader_role_arn  = "arn:aws:iam::111111111111:role/org"
    existing_writer_role_arn  = "arn:aws:iam::111111111111:role/org"
  }

  expect_failures = [terraform_data.inputs_guard]
}

run "created_roles_require_trusted_principals" {
  command = plan

  variables {
    create_rebar_ticket_store = true
  }

  expect_failures = [terraform_data.inputs_guard]
}

run "the_writer_trust_matches_the_reader_trust_without_the_oidc_input" {
  command = plan

  variables {
    create_rebar_ticket_store = true
    trusted_principals        = ["arn:aws:iam::111111111111:role/activity-worker"]
  }

  assert {
    condition     = local.writer_assume_policy == local.assume_policy
    error_message = "Omitting writer_github_oidc_trust changed the writer trust policy the module used to render."
  }

  assert {
    condition     = length(jsondecode(local.writer_assume_policy).Statement) == 1
    error_message = "The writer trusts more than trusted_principals although no OIDC trust was configured."
  }

  assert {
    condition     = aws_iam_role.reader[0].assume_role_policy == local.assume_policy && aws_iam_role.writer[0].assume_role_policy == local.assume_policy
    error_message = "A role does not carry the trust policy the module rendered before the OIDC input existed."
  }
}

run "the_oidc_trust_reaches_the_writer_and_not_the_reader" {
  command = plan

  variables {
    create_rebar_ticket_store = true
    trusted_principals        = ["arn:aws:iam::111111111111:role/activity-worker"]
    writer_github_oidc_trust = {
      provider_arn      = "arn:aws:iam::111111111111:oidc-provider/token.actions.githubusercontent.com"
      subjects          = ["repo:navapbc/agentic-development-platform:ref:refs/heads/main"]
      job_workflow_refs = ["navapbc/agentic-development-platform/.github/workflows/reconcile-bridge.yml@refs/heads/main"]
    }
  }

  assert {
    condition     = length(jsondecode(local.assume_policy).Statement) == 1
    error_message = "The reader gained the OIDC trust, which only the writer needs."
  }

  assert {
    condition     = aws_iam_role.reader[0].assume_role_policy == local.assume_policy && aws_iam_role.writer[0].assume_role_policy == local.writer_assume_policy
    error_message = "A role does not carry the trust policy rendered for it."
  }

  assert {
    condition     = jsondecode(local.writer_assume_policy).Statement[1].Action == "sts:AssumeRoleWithWebIdentity"
    error_message = "The writer trust policy does not carry the web identity statement."
  }

  assert {
    condition     = jsondecode(local.writer_assume_policy).Statement[1].Principal.Federated == "arn:aws:iam::111111111111:oidc-provider/token.actions.githubusercontent.com"
    error_message = "The web identity statement does not name the GitHub OIDC provider."
  }

  assert {
    condition     = jsondecode(local.writer_assume_policy).Statement[1].Condition.StringEquals["token.actions.githubusercontent.com:aud"] == "sts.amazonaws.com"
    error_message = "The web identity statement is not scoped to a token audience."
  }

  assert {
    condition     = jsondecode(local.writer_assume_policy).Statement[1].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == ["repo:navapbc/agentic-development-platform:ref:refs/heads/main"]
    error_message = "The web identity statement is not scoped to the configured subjects."
  }

  assert {
    condition     = jsondecode(local.writer_assume_policy).Statement[1].Condition.StringEquals["token.actions.githubusercontent.com:job_workflow_ref"] == ["navapbc/agentic-development-platform/.github/workflows/reconcile-bridge.yml@refs/heads/main"]
    error_message = "The web identity statement is not scoped to the configured workflow refs."
  }
}

run "the_oidc_trust_omits_the_workflow_ref_condition_when_it_is_unset" {
  command = plan

  variables {
    create_rebar_ticket_store = true
    trusted_principals        = ["arn:aws:iam::111111111111:role/activity-worker"]
    writer_github_oidc_trust = {
      provider_arn = "arn:aws:iam::111111111111:oidc-provider/token.actions.githubusercontent.com"
      subjects     = ["repo:navapbc/agentic-development-platform:ref:refs/heads/main"]
    }
  }

  assert {
    condition     = !can(jsondecode(local.writer_assume_policy).Statement[1].Condition.StringEquals["token.actions.githubusercontent.com:job_workflow_ref"])
    error_message = "An empty job_workflow_refs rendered a condition that matches no token."
  }

  assert {
    condition     = jsondecode(local.writer_assume_policy).Statement[1].Condition.StringEquals["token.actions.githubusercontent.com:aud"] == "sts.amazonaws.com"
    error_message = "The audience condition is missing or is not the default."
  }
}

run "an_unscoped_oidc_subject_is_rejected" {
  command = plan

  variables {
    create_rebar_ticket_store = true
    trusted_principals        = ["arn:aws:iam::111111111111:role/activity-worker"]
    writer_github_oidc_trust = {
      provider_arn = "arn:aws:iam::111111111111:oidc-provider/token.actions.githubusercontent.com"
      subjects     = ["*"]
    }
  }

  expect_failures = [terraform_data.inputs_guard]
}

run "an_oidc_subject_holding_an_embedded_wildcard_is_rejected" {
  command = plan

  variables {
    create_rebar_ticket_store = true
    trusted_principals        = ["arn:aws:iam::111111111111:role/activity-worker"]
    writer_github_oidc_trust = {
      provider_arn = "arn:aws:iam::111111111111:oidc-provider/token.actions.githubusercontent.com"
      subjects     = ["repo:navapbc/agentic-development-platform:ref:refs/heads/*"]
    }
  }

  expect_failures = [terraform_data.inputs_guard]
}

run "an_oidc_workflow_ref_holding_a_wildcard_is_rejected" {
  command = plan

  variables {
    create_rebar_ticket_store = true
    trusted_principals        = ["arn:aws:iam::111111111111:role/activity-worker"]
    writer_github_oidc_trust = {
      provider_arn      = "arn:aws:iam::111111111111:oidc-provider/token.actions.githubusercontent.com"
      subjects          = ["repo:navapbc/agentic-development-platform:ref:refs/heads/main"]
      job_workflow_refs = ["navapbc/agentic-development-platform/.github/workflows/*.yml@refs/heads/main"]
    }
  }

  expect_failures = [terraform_data.inputs_guard]
}

run "an_oidc_trust_without_subjects_is_rejected" {
  command = plan

  variables {
    create_rebar_ticket_store = true
    trusted_principals        = ["arn:aws:iam::111111111111:role/activity-worker"]
    writer_github_oidc_trust = {
      provider_arn = "arn:aws:iam::111111111111:oidc-provider/token.actions.githubusercontent.com"
      subjects     = []
    }
  }

  expect_failures = [terraform_data.inputs_guard]
}

run "an_oidc_trust_alongside_a_supplied_writer_role_is_rejected" {
  command = plan

  variables {
    create_rebar_ticket_store = true
    existing_reader_role_arn  = "arn:aws:iam::111111111111:role/org-reader"
    existing_writer_role_arn  = "arn:aws:iam::111111111111:role/org-writer"
    writer_github_oidc_trust = {
      provider_arn = "arn:aws:iam::111111111111:oidc-provider/token.actions.githubusercontent.com"
      subjects     = ["repo:navapbc/agentic-development-platform:ref:refs/heads/main"]
    }
  }

  expect_failures = [terraform_data.inputs_guard]
}

run "an_oidc_trust_without_an_audience_is_rejected" {
  command = plan

  variables {
    create_rebar_ticket_store = true
    trusted_principals        = ["arn:aws:iam::111111111111:role/activity-worker"]
    writer_github_oidc_trust = {
      provider_arn = "arn:aws:iam::111111111111:oidc-provider/token.actions.githubusercontent.com"
      subjects     = ["repo:navapbc/agentic-development-platform:ref:refs/heads/main"]
      audience     = " "
    }
  }

  expect_failures = [terraform_data.inputs_guard]
}

run "an_oidc_trust_without_a_provider_is_rejected" {
  command = plan

  variables {
    create_rebar_ticket_store = true
    trusted_principals        = ["arn:aws:iam::111111111111:role/activity-worker"]
    writer_github_oidc_trust = {
      provider_arn = ""
      subjects     = ["repo:navapbc/agentic-development-platform:ref:refs/heads/main"]
    }
  }

  expect_failures = [terraform_data.inputs_guard]
}

run "a_blank_oidc_subject_entry_is_rejected" {
  command = plan

  variables {
    create_rebar_ticket_store = true
    trusted_principals        = ["arn:aws:iam::111111111111:role/activity-worker"]
    writer_github_oidc_trust = {
      provider_arn = "arn:aws:iam::111111111111:oidc-provider/token.actions.githubusercontent.com"
      subjects     = ["repo:navapbc/agentic-development-platform:ref:refs/heads/main", "  "]
    }
  }

  expect_failures = [terraform_data.inputs_guard]
}

run "a_blank_oidc_workflow_ref_entry_is_rejected" {
  command = plan

  variables {
    create_rebar_ticket_store = true
    trusted_principals        = ["arn:aws:iam::111111111111:role/activity-worker"]
    writer_github_oidc_trust = {
      provider_arn      = "arn:aws:iam::111111111111:oidc-provider/token.actions.githubusercontent.com"
      subjects          = ["repo:navapbc/agentic-development-platform:ref:refs/heads/main"]
      job_workflow_refs = [""]
    }
  }

  expect_failures = [terraform_data.inputs_guard]
}
