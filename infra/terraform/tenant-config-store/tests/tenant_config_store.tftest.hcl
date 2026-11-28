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
  tenants           = ["adp", "payments"]
  shared_readers = [
    "arn:aws:iam::111111111111:role/agentic-platform-repo-intelligence-task",
    "arn:aws:iam::111111111111:role/agentic-platform-eks-inbound-webhook",
  ]
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

run "each_tenant_prefix_admits_only_principals_tagged_with_the_tenant" {
  command = plan

  variables {
    create_tenant_config_store = true
  }

  assert {
    condition = (
      one([for s in jsondecode(aws_s3_bucket_policy.this[0].policy).Statement : s if s.Sid == "DenyTenantWritesOutsideThePublisherTenant"]).NotResource == [
        "arn:aws:s3:::agentic-platform-tenant-config-abc/tenants/$${aws:PrincipalTag/tenant-config-publisher}/*",
        "arn:aws:s3:::agentic-platform-tenant-config-abc/claims/slack-channel/*/$${aws:PrincipalTag/tenant-config-publisher}",
        "arn:aws:s3:::agentic-platform-tenant-config-abc/claims/slack-own-channel/*/$${aws:PrincipalTag/tenant-config-publisher}",
        "arn:aws:s3:::agentic-platform-tenant-config-abc/claims/slack-app/*/$${aws:PrincipalTag/tenant-config-publisher}",
        "arn:aws:s3:::agentic-platform-tenant-config-abc/claims/jira-project/*/$${aws:PrincipalTag/tenant-config-publisher}",
        "arn:aws:s3:::agentic-platform-tenant-config-abc/claims/registry-username/*/$${aws:PrincipalTag/tenant-config-publisher}",
        "arn:aws:s3:::agentic-platform-tenant-config-abc/claims/_owners/$${aws:PrincipalTag/tenant-config-publisher}.json",
        "arn:aws:s3:::agentic-platform-tenant-config-abc/platform/*",
      ] &&
      one([for s in jsondecode(aws_s3_bucket_policy.this[0].policy).Statement : s if s.Sid == "DenyTenantWritesOutsideThePublisherTenant"]).Condition.ArnNotLike["aws:PrincipalArn"] == ["arn:aws:iam::111111111111:user/platform-admin"] &&
      one([for s in jsondecode(aws_s3_bucket_policy.this[0].policy).Statement : s if s.Sid == "DenyTenantWritesOutsideThePublisherTenant"]).Action == ["s3:PutObject*", "s3:DeleteObject*", "s3:RestoreObject", "s3:AbortMultipartUpload"]
    )
    error_message = "A tenant prefix admits a writer other than a principal tagged as its publisher and the admins."
  }

  assert {
    condition = (
      one([for s in jsondecode(aws_s3_bucket_policy.this[0].policy).Statement : s if s.Sid == "DenyTenantReadsOutsideThePrincipalTenant"]).NotResource == [
        "arn:aws:s3:::agentic-platform-tenant-config-abc/tenants/$${aws:PrincipalTag/tenant-config-reader}/*",
        "arn:aws:s3:::agentic-platform-tenant-config-abc/tenants/$${aws:PrincipalTag/tenant-config-publisher}/*",
        "arn:aws:s3:::agentic-platform-tenant-config-abc/claims/_owners/$${aws:PrincipalTag/tenant-config-publisher}.json",
        "arn:aws:s3:::agentic-platform-tenant-config-abc/platform/*",
      ] &&
      one([for s in jsondecode(aws_s3_bucket_policy.this[0].policy).Statement : s if s.Sid == "DenyTenantReadsOutsideThePrincipalTenant"]).Condition.ArnNotLike["aws:PrincipalArn"] == [
        "arn:aws:iam::111111111111:role/agentic-platform-repo-intelligence-task",
        "arn:aws:iam::111111111111:role/agentic-platform-eks-inbound-webhook",
        "arn:aws:iam::111111111111:user/platform-admin",
      ]
    )
    error_message = "A tenant prefix admits a reader other than a principal tagged with the tenant, the shared readers, and the admins."
  }

  assert {
    condition = !contains(
      one([for s in jsondecode(aws_s3_bucket_policy.this[0].policy).Statement : s if s.Sid == "DenyTenantWritesOutsideThePublisherTenant"]).NotResource,
      "arn:aws:s3:::agentic-platform-tenant-config-abc/tenants/$${aws:PrincipalTag/tenant-config-reader}/*",
    )
    error_message = "A principal tagged as a tenant's reader may write the tenant's bundle."
  }

  assert {
    condition     = output.tenant_principal_tag_keys == { reader = "tenant-config-reader", publisher = "tenant-config-publisher" }
    error_message = "The module does not report the principal tag keys its bucket policy matches."
  }

  assert {
    condition = one([for s in jsondecode(aws_s3_bucket_policy.this[0].policy).Statement : s if s.Sid == "DenyObjectsOutsideTheTenantClaimAndPlatformKeys"]).NotResource == [
      "arn:aws:s3:::agentic-platform-tenant-config-abc/tenants/*",
      "arn:aws:s3:::agentic-platform-tenant-config-abc/claims/slack-channel/*/*",
      "arn:aws:s3:::agentic-platform-tenant-config-abc/claims/slack-own-channel/*/*",
      "arn:aws:s3:::agentic-platform-tenant-config-abc/claims/slack-app/*/*",
      "arn:aws:s3:::agentic-platform-tenant-config-abc/claims/jira-project/*/*",
      "arn:aws:s3:::agentic-platform-tenant-config-abc/claims/registry-username/*/*",
      "arn:aws:s3:::agentic-platform-tenant-config-abc/claims/_owners/*.json",
      "arn:aws:s3:::agentic-platform-tenant-config-abc/platform/image_exceptions.yaml",
      "arn:aws:s3:::agentic-platform-tenant-config-abc/platform/image_policy.yaml",
      "arn:aws:s3:::agentic-platform-tenant-config-abc/platform/claims-protocol.json",
    ]
    error_message = "The bucket admits an object outside tenants/, the claim keys of the four kinds, the claim indexes, and the three platform keys."
  }

  assert {
    condition     = output.tenant_prefixes == { adp = "tenants/adp/", payments = "tenants/payments/" }
    error_message = "The tenant prefixes output does not name each tenant's prefix."
  }
}

run "the_bucket_policy_does_not_grow_with_the_tenant_count" {
  command = plan

  variables {
    create_tenant_config_store = true
    exception_reviewers        = ["arn:aws:iam::111111111111:role/agentic-platform-github-image-publisher"]
    tenants                    = [for index in range(200) : format("tenant%010d", index)]
  }

  assert {
    condition = (
      length(jsondecode(aws_s3_bucket_policy.this[0].policy).Statement) == 11 &&
      !strcontains(aws_s3_bucket_policy.this[0].policy, "tenant0000000199") &&
      length(aws_s3_bucket_policy.this[0].policy) < 6144
    )
    error_message = "The bucket policy grows with the tenant count, toward the 20 KB limit of a bucket policy."
  }

  assert {
    condition     = length(output.tenant_prefixes) == 200
    error_message = "The tenant prefixes output does not name every tenant."
  }
}

run "without_admins_or_shared_readers_the_tenant_denies_carry_no_empty_condition" {
  command = plan

  variables {
    create_tenant_config_store = true
    admin_principals           = []
    shared_readers             = []
  }

  assert {
    condition = alltrue([
      for s in jsondecode(aws_s3_bucket_policy.this[0].policy).Statement : !contains(keys(s), "Condition") if contains(["DenyTenantWritesOutsideThePublisherTenant", "DenyTenantReadsOutsideThePrincipalTenant"], s.Sid)
    ])
    error_message = "A tenant deny carries a condition with no principal, which IAM refuses."
  }

  assert {
    condition     = keys(one([for s in jsondecode(aws_s3_bucket_policy.this[0].policy).Statement : s if s.Sid == "DenyClaimListsBeyondOneDigest"]).Condition) == ["StringNotLike"]
    error_message = "The claim list deny carries a principal condition with no admin, which IAM refuses."
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
    condition = (
      contains(
        one([for s in jsondecode(aws_s3_bucket_policy.this[0].policy).Statement : s if s.Sid == "DenyTenantReadsOutsideThePrincipalTenant"]).Condition.ArnNotLike["aws:PrincipalArn"],
        "arn:aws:iam::111111111111:role/agentic-platform-github-image-publisher",
      ) &&
      !contains(
        one([for s in jsondecode(aws_s3_bucket_policy.this[0].policy).Statement : s if s.Sid == "DenyTenantWritesOutsideThePublisherTenant"]).Condition.ArnNotLike["aws:PrincipalArn"],
        "arn:aws:iam::111111111111:role/agentic-platform-github-image-publisher",
      )
    )
    error_message = "The exception reviewer cannot read the tenant prefixes, or may write them."
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

run "a_publisher_writes_and_deletes_only_its_own_claims" {
  command = plan

  variables {
    create_tenant_config_store = true
  }

  assert {
    condition = (
      length([for r in one([for s in jsondecode(aws_s3_bucket_policy.this[0].policy).Statement : s if s.Sid == "DenyTenantWritesOutsideThePublisherTenant"]).NotResource : r if strcontains(r, "/claims/")]) == 6 &&
      alltrue([
        for r in one([for s in jsondecode(aws_s3_bucket_policy.this[0].policy).Statement : s if s.Sid == "DenyTenantWritesOutsideThePublisherTenant"]).NotResource :
        endswith(r, "/$${aws:PrincipalTag/tenant-config-publisher}") || endswith(r, "/claims/_owners/$${aws:PrincipalTag/tenant-config-publisher}.json") if strcontains(r, "/claims/")
      ]) &&
      contains(one([for s in jsondecode(aws_s3_bucket_policy.this[0].policy).Statement : s if s.Sid == "DenyTenantWritesOutsideThePublisherTenant"]).Action, "s3:DeleteObject*")
    )
    error_message = "A publisher may write or delete a claim marker or claim index named for another tenant."
  }

  assert {
    condition = (
      one([for s in jsondecode(aws_s3_bucket_policy.this[0].policy).Statement : s if s.Sid == "DenyClaimMarkerCreatesWithoutIfNoneMatch"]).Action == "s3:PutObject" &&
      one([for s in jsondecode(aws_s3_bucket_policy.this[0].policy).Statement : s if s.Sid == "DenyClaimMarkerCreatesWithoutIfNoneMatch"]).Resource == [
        "arn:aws:s3:::agentic-platform-tenant-config-abc/claims/slack-channel/*",
        "arn:aws:s3:::agentic-platform-tenant-config-abc/claims/slack-own-channel/*",
        "arn:aws:s3:::agentic-platform-tenant-config-abc/claims/slack-app/*",
        "arn:aws:s3:::agentic-platform-tenant-config-abc/claims/jira-project/*",
        "arn:aws:s3:::agentic-platform-tenant-config-abc/claims/registry-username/*",
      ] &&
      one([for s in jsondecode(aws_s3_bucket_policy.this[0].policy).Statement : s if s.Sid == "DenyClaimMarkerCreatesWithoutIfNoneMatch"]).Condition == { Null = { "s3:if-none-match" = "true" } }
    )
    error_message = "A claim marker may be written without If-None-Match, or the claim index requires it."
  }

  assert {
    condition = (
      one([for s in jsondecode(aws_s3_bucket_policy.this[0].policy).Statement : s if s.Sid == "DenyClaimKeysWithExtraSegments"]).Resource == "arn:aws:s3:::agentic-platform-tenant-config-abc/claims/*/*/*/*" &&
      one([for s in jsondecode(aws_s3_bucket_policy.this[0].policy).Statement : s if s.Sid == "DenyClaimKeysWithExtraSegments"]).Action == ["s3:PutObject*"] &&
      !contains(keys(one([for s in jsondecode(aws_s3_bucket_policy.this[0].policy).Statement : s if s.Sid == "DenyClaimKeysWithExtraSegments"])), "Condition")
    )
    error_message = "The bucket admits a claim key with five or more segments."
  }
}

run "a_claim_list_names_one_digest_unless_an_admin_lists" {
  command = plan

  variables {
    create_tenant_config_store = true
  }

  assert {
    condition = (
      one([for s in jsondecode(aws_s3_bucket_policy.this[0].policy).Statement : s if s.Sid == "DenyClaimListsBeyondOneDigest"]).Action == ["s3:ListBucket", "s3:ListBucketVersions"] &&
      one([for s in jsondecode(aws_s3_bucket_policy.this[0].policy).Statement : s if s.Sid == "DenyClaimListsBeyondOneDigest"]).Resource == "arn:aws:s3:::agentic-platform-tenant-config-abc" &&
      one([for s in jsondecode(aws_s3_bucket_policy.this[0].policy).Statement : s if s.Sid == "DenyClaimListsBeyondOneDigest"]).Condition.StringNotLike["s3:prefix"] == [
        "claims/slack-channel/*/",
        "claims/slack-own-channel/*/",
        "claims/slack-app/*/",
        "claims/jira-project/*/",
        "claims/registry-username/*/",
        "tenants/*",
        "platform/*",
      ]
    )
    error_message = "A list without a prefix, or with a claims prefix that does not name one digest and end in a slash, is admitted."
  }

  assert {
    condition     = one([for s in jsondecode(aws_s3_bucket_policy.this[0].policy).Statement : s if s.Sid == "DenyClaimListsBeyondOneDigest"]).Condition.ArnNotLike["aws:PrincipalArn"] == ["arn:aws:iam::111111111111:user/platform-admin"]
    error_message = "An admin cannot list every key under claims/."
  }
}

run "without_the_claims_protocol_a_bundle_upload_needs_no_tag" {
  command = plan

  variables {
    create_tenant_config_store = true
  }

  assert {
    condition     = length([for s in jsondecode(aws_s3_bucket_policy.this[0].policy).Statement : s if s.Sid == "DenyBundleUploadsWithoutTheClaimsProtocolTag"]) == 0
    error_message = "The bucket policy requires the claims protocol tag while enforce_claims_protocol is off."
  }
}

run "the_claims_protocol_denies_an_untagged_bundle_upload" {
  command = plan

  variables {
    create_tenant_config_store = true
    enforce_claims_protocol    = true
  }

  assert {
    condition = (
      one([for s in jsondecode(aws_s3_bucket_policy.this[0].policy).Statement : s if s.Sid == "DenyBundleUploadsWithoutTheClaimsProtocolTag"]).Action == "s3:PutObject" &&
      one([for s in jsondecode(aws_s3_bucket_policy.this[0].policy).Statement : s if s.Sid == "DenyBundleUploadsWithoutTheClaimsProtocolTag"]).Resource == "arn:aws:s3:::agentic-platform-tenant-config-abc/tenants/*" &&
      one([for s in jsondecode(aws_s3_bucket_policy.this[0].policy).Statement : s if s.Sid == "DenyBundleUploadsWithoutTheClaimsProtocolTag"]).Condition == { StringNotEquals = { "s3:RequestObjectTag/claims-protocol" = "1" } }
    )
    error_message = "A bundle upload without the tag claims-protocol=1 is admitted while enforce_claims_protocol is on."
  }
}

run "the_claims_backfilled_object_requires_the_claims_protocol" {
  command = plan

  variables {
    create_tenant_config_store = true
    claims_backfilled          = true
  }

  expect_failures = [var.claims_backfilled]
}

run "without_claims_backfilled_no_claims_protocol_object_is_written" {
  command = plan

  variables {
    create_tenant_config_store = true
    enforce_claims_protocol    = true
  }

  assert {
    condition     = length(aws_s3_object.claims_protocol) == 0
    error_message = "The claims-protocol object is written before claims_backfilled is set."
  }
}

run "claims_backfilled_writes_the_claims_protocol_object_under_an_allowed_key" {
  command = plan

  variables {
    create_tenant_config_store = true
    enforce_claims_protocol    = true
    claims_backfilled          = true
  }

  assert {
    condition = (
      aws_s3_object.claims_protocol[0].key == "platform/claims-protocol.json" &&
      aws_s3_object.claims_protocol[0].server_side_encryption == "aws:kms" &&
      aws_s3_object.claims_protocol[0].kms_key_id == "arn:aws:kms:us-east-1:111111111111:key/config" &&
      output.claims_protocol_key == "platform/claims-protocol.json"
    )
    error_message = "The claims-protocol object is not written, encrypted with the bucket key, at platform/claims-protocol.json."
  }

  assert {
    condition = contains(
      one([for s in jsondecode(aws_s3_bucket_policy.this[0].policy).Statement : s if s.Sid == "DenyObjectsOutsideTheTenantClaimAndPlatformKeys"]).NotResource,
      "arn:aws:s3:::agentic-platform-tenant-config-abc/platform/claims-protocol.json",
    )
    error_message = "The bucket policy denies the claims-protocol object's key."
  }
}
