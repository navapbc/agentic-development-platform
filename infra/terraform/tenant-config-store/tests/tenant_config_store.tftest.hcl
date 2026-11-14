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
    id  = "agentic-platform-tenant-config-abc"
    arn = "arn:aws:s3:::agentic-platform-tenant-config-abc"
  }
}

override_resource {
  target          = aws_kms_key.this
  override_during = plan
  values          = { arn = "arn:aws:kms:us-east-1:111111111111:key/config" }
}

variables {
  name_prefix       = "agentic-platform"
  access_log_bucket = "agentic-platform-access-logs-111111111111"
  admin_principals  = ["arn:aws:iam::111111111111:user/platform-admin"]
  tenants = {
    adp = {
      publishers = ["arn:aws:iam::111111111111:role/agentic-platform-tenant-adp-config-publisher"]
      readers    = ["arn:aws:iam::111111111111:role/agentic-platform-eks-tenant-adp"]
    }
    payments = {
      publishers = ["arn:aws:iam::111111111111:role/agentic-platform-tenant-payments-config-publisher"]
      readers    = ["arn:aws:iam::111111111111:role/agentic-platform-eks-tenant-payments"]
    }
  }
}

run "disabled_module_creates_nothing" {
  command = plan

  variables {
    create_tenant_config_store = false
  }

  assert {
    condition     = length(aws_s3_bucket.this) == 0 && length(aws_kms_key.this) == 0
    error_message = "The disabled module planned resources."
  }
}

run "the_bucket_is_versioned_encrypted_private_and_logged" {
  command = plan

  variables {
    create_tenant_config_store = true
  }

  assert {
    condition = (
      aws_s3_bucket_versioning.this[0].versioning_configuration[0].status == "Enabled" &&
      one(aws_s3_bucket_server_side_encryption_configuration.this[0].rule).apply_server_side_encryption_by_default[0].sse_algorithm == "aws:kms" &&
      aws_s3_bucket_public_access_block.this[0].restrict_public_buckets &&
      aws_s3_bucket_logging.this[0].target_bucket == "agentic-platform-access-logs-111111111111" &&
      aws_kms_key.this[0].enable_key_rotation
    )
    error_message = "The bucket is not versioned, KMS-encrypted, private, and logged."
  }
}

run "each_tenant_prefix_admits_only_its_own_principals" {
  command = plan

  variables {
    create_tenant_config_store = true
  }

  assert {
    condition = (
      one([for s in jsondecode(aws_s3_bucket_policy.this[0].policy).Statement : s if s.Sid == "DenyWritesOutsideTheAdpPublishers"]).Resource == "arn:aws:s3:::agentic-platform-tenant-config-abc/tenants/adp/*" &&
      one([for s in jsondecode(aws_s3_bucket_policy.this[0].policy).Statement : s if s.Sid == "DenyWritesOutsideTheAdpPublishers"]).Condition.ArnNotLike["aws:PrincipalArn"] == [
        "arn:aws:iam::111111111111:role/agentic-platform-tenant-adp-config-publisher",
        "arn:aws:iam::111111111111:user/platform-admin",
      ]
    )
    error_message = "The adp prefix admits a writer other than its publisher and the admins."
  }

  assert {
    condition = !contains(
      one([for s in jsondecode(aws_s3_bucket_policy.this[0].policy).Statement : s if s.Sid == "DenyReadsOutsideThePaymentsReaders"]).Condition.ArnNotLike["aws:PrincipalArn"],
      "arn:aws:iam::111111111111:role/agentic-platform-eks-tenant-adp",
    )
    error_message = "The adp worker may read the payments prefix."
  }

  assert {
    condition = one([for s in jsondecode(aws_s3_bucket_policy.this[0].policy).Statement : s if s.Sid == "DenyObjectsOutsideTheTenantAndPlatformKeys"]).NotResource == [
      "arn:aws:s3:::agentic-platform-tenant-config-abc/tenants/*",
      "arn:aws:s3:::agentic-platform-tenant-config-abc/platform/image_exceptions.yaml",
      "arn:aws:s3:::agentic-platform-tenant-config-abc/platform/image_policy.yaml",
    ]
    error_message = "The bucket admits an object outside tenants/ and the two platform keys."
  }

  assert {
    condition     = output.tenant_prefixes == { adp = "tenants/adp/", payments = "tenants/payments/" }
    error_message = "The tenant prefixes output does not name each tenant's prefix."
  }
}

run "the_key_names_no_tenant_principal" {
  command = plan

  variables {
    create_tenant_config_store = true
  }

  assert {
    condition     = length(jsondecode(aws_kms_key.this[0].policy).Statement) == 1
    error_message = "The key policy names a principal beyond the account."
  }
}

run "the_platform_exceptions_file_starts_empty_and_only_its_key_is_readable" {
  command = plan

  variables {
    create_tenant_config_store = true
  }

  assert {
    condition = (
      aws_s3_object.platform_exceptions[0].key == "platform/image_exceptions.yaml" &&
      aws_s3_object.platform_exceptions[0].content == "config_version: 1\nexceptions: []\n" &&
      aws_s3_object.platform_exceptions[0].kms_key_id == "arn:aws:kms:us-east-1:111111111111:key/config"
    )
    error_message = "The platform exceptions file does not start as an empty, KMS-encrypted list."
  }

  assert {
    condition     = length(aws_s3_object.platform_image_policy) == 0
    error_message = "The image policy object exists without publish_image_policy."
  }

  assert {
    condition = (
      jsondecode(aws_iam_policy.platform_exceptions_read[0].policy).Statement[0].Resource == "arn:aws:s3:::agentic-platform-tenant-config-abc/platform/image_exceptions.yaml" &&
      jsondecode(aws_iam_policy.platform_exceptions_read[0].policy).Statement[0].Action == ["s3:GetObject", "s3:GetObjectVersion"] &&
      jsondecode(aws_iam_policy.platform_exceptions_read[0].policy).Statement[1].Condition.StringEquals["kms:ViaService"] == "s3.us-east-1.amazonaws.com"
    )
    error_message = "The worker read policy reaches beyond the platform exceptions file."
  }

  assert {
    condition     = length([for s in jsondecode(aws_s3_bucket_policy.this[0].policy).Statement : s if s.Sid == "DenyExceptionReviewersOtherObjects"]) == 0
    error_message = "The bucket policy names exception reviewers when none is configured."
  }
}

run "an_exception_reviewer_reads_only_the_exceptions_files_and_the_platform_keys" {
  command = plan

  variables {
    create_tenant_config_store = true
    exception_reviewers        = ["arn:aws:iam::111111111111:role/agentic-platform-github-image-publisher"]
    publish_image_policy       = true
    image_policy               = "config_version: 1\n"
  }

  assert {
    condition = contains(
      one([for s in jsondecode(aws_s3_bucket_policy.this[0].policy).Statement : s if s.Sid == "DenyReadsOutsideTheAdpReaders"]).Condition.ArnNotLike["aws:PrincipalArn"],
      "arn:aws:iam::111111111111:role/agentic-platform-github-image-publisher",
    )
    error_message = "The exception reviewer cannot read the adp prefix."
  }

  assert {
    condition = (
      one([for s in jsondecode(aws_s3_bucket_policy.this[0].policy).Statement : s if s.Sid == "DenyExceptionReviewersOtherObjects"]).NotResource == [
        "arn:aws:s3:::agentic-platform-tenant-config-abc/tenants/*/image_exceptions.yaml",
        "arn:aws:s3:::agentic-platform-tenant-config-abc/platform/*",
      ] &&
      one([for s in jsondecode(aws_s3_bucket_policy.this[0].policy).Statement : s if s.Sid == "DenyExceptionReviewersOtherObjects"]).Condition.ArnLike["aws:PrincipalArn"] == ["arn:aws:iam::111111111111:role/agentic-platform-github-image-publisher"]
    )
    error_message = "The exception reviewer may read a tenant object other than its exceptions file."
  }

  assert {
    condition     = aws_s3_object.platform_image_policy[0].key == "platform/image_policy.yaml" && aws_s3_object.platform_image_policy[0].content == "config_version: 1\n"
    error_message = "The image policy object does not hold image_policy."
  }
}
