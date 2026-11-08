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
    condition     = one([for s in jsondecode(aws_s3_bucket_policy.this[0].policy).Statement : s if s.Sid == "DenyObjectsOutsideATenantPrefix"]).NotResource == "arn:aws:s3:::agentic-platform-tenant-config-abc/tenants/*"
    error_message = "The bucket admits an object outside tenants/."
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
