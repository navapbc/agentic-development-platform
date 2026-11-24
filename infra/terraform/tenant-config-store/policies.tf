locals {
  # IAM grants each tenant's worker and publisher the key through the S3 service, so the key
  # policy names no tenant principal and holds no role that may not exist yet.
  key_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "EnableAccountKeyAdministration"
      Effect    = "Allow"
      Principal = { AWS = "arn:${local.partition}:iam::${local.account_id}:root" }
      Action    = "kms:*"
      Resource  = "*"
    }]
  })

  # A tenant's principals carry its name in a principal tag, so two fixed statements isolate every
  # tenant prefix and the policy does not grow with the tenant count. A NotResource entry whose tag
  # the principal lacks matches no object, so an untagged principal is denied every tenant prefix.
  # Keys outside tenants/, claims/, and platform/ fall under the denies too, and the bucket admits
  # no object there.
  tenant_reader_prefix    = "${local.bucket_arn}/tenants/$${aws:PrincipalTag/${local.tenant_tag_keys.reader}}/*"
  tenant_publisher_prefix = "${local.bucket_arn}/tenants/$${aws:PrincipalTag/${local.tenant_tag_keys.publisher}}/*"
  tenant_write_exempt     = var.admin_principals
  tenant_read_exempt      = distinct(concat(var.shared_readers, var.exception_reviewers, var.admin_principals))

  # A claim is the empty marker claims/<kind>/<digest>/<tenant>, and each tenant's publisher keeps
  # the index claims/_owners/<tenant>.json. S3 cannot condition a delete on the object's tags, so
  # the owner is the last key segment and the tenant write deny matches it to the principal tag.
  claim_kinds                = ["slack-channel", "slack-own-channel", "slack-app", "jira-project", "registry-username"]
  claim_markers              = [for kind in local.claim_kinds : "${local.bucket_arn}/claims/${kind}/*"]
  claim_marker_keys          = [for kind in local.claim_kinds : "${local.bucket_arn}/claims/${kind}/*/*"]
  publisher_claim_markers    = [for kind in local.claim_kinds : "${local.bucket_arn}/claims/${kind}/*/$${aws:PrincipalTag/${local.tenant_tag_keys.publisher}}"]
  publisher_claim_index      = "${local.bucket_arn}/claims/_owners/$${aws:PrincipalTag/${local.tenant_tag_keys.publisher}}.json"
  claim_digest_list_prefixes = [for kind in local.claim_kinds : "claims/${kind}/*/"]
  claims_protocol_tag        = { key = "claims-protocol", value = "1" }

  tenant_statements = [
    merge(
      {
        Sid       = "DenyTenantWritesOutsideThePublisherTenant"
        Effect    = "Deny"
        Principal = "*"
        Action    = ["s3:PutObject*", "s3:DeleteObject*", "s3:RestoreObject", "s3:AbortMultipartUpload"]
        NotResource = concat(
          [local.tenant_publisher_prefix],
          local.publisher_claim_markers,
          [local.publisher_claim_index, "${local.bucket_arn}/platform/*"],
        )
      },
      length(local.tenant_write_exempt) == 0 ? {} : { Condition = { ArnNotLike = { "aws:PrincipalArn" = local.tenant_write_exempt } } },
    ),
    merge(
      {
        Sid         = "DenyTenantReadsOutsideThePrincipalTenant"
        Effect      = "Deny"
        Principal   = "*"
        Action      = ["s3:GetObject*", "s3:ListMultipartUploadParts"]
        NotResource = [local.tenant_reader_prefix, local.tenant_publisher_prefix, local.publisher_claim_index, "${local.bucket_arn}/platform/*"]
      },
      length(local.tenant_read_exempt) == 0 ? {} : { Condition = { ArnNotLike = { "aws:PrincipalArn" = local.tenant_read_exempt } } },
    ),
  ]

  claim_statements = concat(
    [
      {
        Sid       = "DenyClaimMarkerCreatesWithoutIfNoneMatch"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:PutObject"
        Resource  = local.claim_markers
        Condition = { Null = { "s3:if-none-match" = "true" } }
      },
      # A policy wildcard also matches a slash, so the claim key patterns admit keys with more
      # than four segments unless this deny refuses them.
      {
        Sid       = "DenyClaimKeysWithExtraSegments"
        Effect    = "Deny"
        Principal = "*"
        Action    = ["s3:PutObject*"]
        Resource  = "${local.bucket_arn}/claims/*/*/*/*"
      },
      # A negated operator also matches a request without a prefix, so a list must name a tenant
      # or platform prefix or exactly one claim digest, and a caller learns the owners of a claim
      # only for a value it already knows.
      {
        Sid       = "DenyClaimListsBeyondOneDigest"
        Effect    = "Deny"
        Principal = "*"
        Action    = ["s3:ListBucket", "s3:ListBucketVersions"]
        Resource  = local.bucket_arn
        Condition = merge(
          { StringNotLike = { "s3:prefix" = concat(local.claim_digest_list_prefixes, ["tenants/*", "platform/*"]) } },
          length(var.admin_principals) == 0 ? {} : { ArnNotLike = { "aws:PrincipalArn" = var.admin_principals } },
        )
      },
    ],
    var.enforce_claims_protocol ? [
      {
        Sid       = "DenyBundleUploadsWithoutTheClaimsProtocolTag"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:PutObject"
        Resource  = "${local.bucket_arn}/tenants/*"
        Condition = { StringNotEquals = { "s3:RequestObjectTag/${local.claims_protocol_tag.key}" = local.claims_protocol_tag.value } }
      },
    ] : [],
  )

  # The exception reviewers pass the tenant read deny above, so this deny keeps them to the
  # exceptions files and the platform keys.
  reviewer_statements = length(var.exception_reviewers) == 0 ? [] : [
    {
      Sid       = "DenyExceptionReviewersOtherObjects"
      Effect    = "Deny"
      Principal = "*"
      Action    = ["s3:GetObject*", "s3:ListMultipartUploadParts"]
      NotResource = [
        "${local.bucket_arn}/tenants/*/image_exceptions.yaml",
        "${local.bucket_arn}/platform/*",
      ]
      Condition = { ArnLike = { "aws:PrincipalArn" = var.exception_reviewers } }
    },
  ]

  bucket_policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [
        {
          Sid       = "DenyNonTls"
          Effect    = "Deny"
          Principal = "*"
          Action    = "s3:*"
          Resource  = [local.bucket_arn, "${local.bucket_arn}/*"]
          Condition = { Bool = { "aws:SecureTransport" = "false" } }
        },
        {
          Sid       = "DenyTlsBelow12"
          Effect    = "Deny"
          Principal = "*"
          Action    = "s3:*"
          Resource  = [local.bucket_arn, "${local.bucket_arn}/*"]
          Condition = { NumericLessThan = { "s3:TlsVersion" = "1.2" } }
        },
        {
          Sid       = "DenyObjectsOutsideTheTenantClaimAndPlatformKeys"
          Effect    = "Deny"
          Principal = "*"
          Action    = ["s3:PutObject*"]
          NotResource = concat(
            ["${local.bucket_arn}/tenants/*"],
            local.claim_marker_keys,
            [
              "${local.bucket_arn}/claims/_owners/*.json",
              "${local.bucket_arn}/${local.platform_exceptions_key}",
              "${local.bucket_arn}/${local.platform_policy_key}",
              "${local.bucket_arn}/${local.claims_protocol_key}",
            ],
          )
        },
        # An IfExists operator matches when the header is absent, so each deny pairs a Null check
        # with the comparison to deny only a request that names another encryption.
        {
          Sid       = "DenyEncryptionOtherThanKms"
          Effect    = "Deny"
          Principal = "*"
          Action    = "s3:PutObject"
          Resource  = "${local.bucket_arn}/*"
          Condition = {
            Null            = { "s3:x-amz-server-side-encryption" = "false" }
            StringNotEquals = { "s3:x-amz-server-side-encryption" = "aws:kms" }
          }
        },
        {
          Sid       = "DenyAnotherKmsKey"
          Effect    = "Deny"
          Principal = "*"
          Action    = "s3:PutObject"
          Resource  = "${local.bucket_arn}/*"
          Condition = {
            Null            = { "s3:x-amz-server-side-encryption-aws-kms-key-id" = "false" }
            StringNotEquals = { "s3:x-amz-server-side-encryption-aws-kms-key-id" = local.kms_key_arn }
          }
        },
      ],
      local.tenant_statements,
      local.claim_statements,
      local.reviewer_statements,
    )
  })
}
