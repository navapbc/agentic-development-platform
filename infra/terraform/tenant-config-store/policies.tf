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
  # Keys outside tenants/ and platform/ fall under the denies too, and the bucket admits no
  # object there.
  tenant_reader_prefix    = "${local.bucket_arn}/tenants/$${aws:PrincipalTag/${local.tenant_tag_keys.reader}}/*"
  tenant_publisher_prefix = "${local.bucket_arn}/tenants/$${aws:PrincipalTag/${local.tenant_tag_keys.publisher}}/*"
  tenant_write_exempt     = var.admin_principals
  tenant_read_exempt      = distinct(concat(var.shared_readers, var.exception_reviewers, var.admin_principals))

  tenant_statements = [
    merge(
      {
        Sid         = "DenyTenantWritesOutsideThePublisherTenant"
        Effect      = "Deny"
        Principal   = "*"
        Action      = ["s3:PutObject*", "s3:DeleteObject*", "s3:RestoreObject", "s3:AbortMultipartUpload"]
        NotResource = [local.tenant_publisher_prefix, "${local.bucket_arn}/platform/*"]
      },
      length(local.tenant_write_exempt) == 0 ? {} : { Condition = { ArnNotLike = { "aws:PrincipalArn" = local.tenant_write_exempt } } },
    ),
    merge(
      {
        Sid         = "DenyTenantReadsOutsideThePrincipalTenant"
        Effect      = "Deny"
        Principal   = "*"
        Action      = ["s3:GetObject*", "s3:ListMultipartUploadParts"]
        NotResource = [local.tenant_reader_prefix, local.tenant_publisher_prefix, "${local.bucket_arn}/platform/*"]
      },
      length(local.tenant_read_exempt) == 0 ? {} : { Condition = { ArnNotLike = { "aws:PrincipalArn" = local.tenant_read_exempt } } },
    ),
  ]

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
          Sid       = "DenyObjectsOutsideTheTenantAndPlatformKeys"
          Effect    = "Deny"
          Principal = "*"
          Action    = ["s3:PutObject*"]
          NotResource = [
            "${local.bucket_arn}/tenants/*",
            "${local.bucket_arn}/${local.platform_exceptions_key}",
            "${local.bucket_arn}/${local.platform_policy_key}",
          ]
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
      local.reviewer_statements,
    )
  })
}
