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

  # Each tenant's prefix denies every principal outside its own lists, so a publisher role of one
  # tenant cannot read or write another tenant's bundle even with a broader IAM policy.
  tenant_statements = flatten([
    for tenant, access in var.tenants : [
      {
        Sid       = "DenyWritesOutsideThe${title(tenant)}Publishers"
        Effect    = "Deny"
        Principal = "*"
        Action    = ["s3:PutObject*", "s3:DeleteObject*", "s3:RestoreObject", "s3:AbortMultipartUpload"]
        Resource  = "${local.bucket_arn}/tenants/${tenant}/*"
        Condition = { ArnNotLike = { "aws:PrincipalArn" = concat(access.publishers, var.admin_principals) } }
      },
      {
        Sid       = "DenyReadsOutsideThe${title(tenant)}Readers"
        Effect    = "Deny"
        Principal = "*"
        Action    = ["s3:GetObject*", "s3:ListMultipartUploadParts"]
        Resource  = "${local.bucket_arn}/tenants/${tenant}/*"
        Condition = { ArnNotLike = { "aws:PrincipalArn" = concat(access.readers, access.publishers, var.admin_principals) } }
      },
    ]
  ])

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
          Sid         = "DenyObjectsOutsideATenantPrefix"
          Effect      = "Deny"
          Principal   = "*"
          Action      = ["s3:PutObject*"]
          NotResource = "${local.bucket_arn}/tenants/*"
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
    )
  })
}
