locals {
  s3_via_service = "s3.${local.region}.amazonaws.com"
  bucket_context = [local.bucket_arn, "${local.bucket_arn}/*"]

  key_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "EnableAccountKeyAdministration"
        Effect    = "Allow"
        Principal = { AWS = "arn:${local.partition}:iam::${local.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        Sid       = "DecryptThroughTheBucket"
        Effect    = "Allow"
        Principal = { AWS = local.object_readers }
        Action    = "kms:Decrypt"
        Resource  = "*"
        Condition = {
          StringEquals = { "kms:ViaService" = local.s3_via_service }
          StringLike   = { "kms:EncryptionContext:aws:s3:arn" = local.bucket_context }
        }
      },
      {
        Sid       = "EncryptThroughTheBucket"
        Effect    = "Allow"
        Principal = { AWS = local.object_writers }
        Action    = "kms:GenerateDataKey"
        Resource  = "*"
        Condition = {
          StringEquals = { "kms:ViaService" = local.s3_via_service }
          StringLike   = { "kms:EncryptionContext:aws:s3:arn" = local.bucket_context }
        }
      },
    ]
  })

  bucket_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
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
      # Bucket-level actions stay with IAM. HeadBucket is authorized as s3:ListBucket, and the AWS
      # provider reads a denied HeadBucket as a deleted bucket and plans a replacement, which orphans
      # the store. Listing reveals only ref names and bundle hashes.
      {
        Sid       = "DenyObjectAccessOutsideTheStoreRoles"
        Effect    = "Deny"
        Principal = "*"
        Action = [
          "s3:GetObject*",
          "s3:PutObject*",
          "s3:DeleteObject*",
          "s3:RestoreObject",
          "s3:ListMultipartUploadParts",
          "s3:AbortMultipartUpload",
        ]
        Resource  = "${local.bucket_arn}/*"
        Condition = { ArnNotLike = { "aws:PrincipalArn" = local.object_readers } }
      },
      {
        Sid       = "DenyWritesByTheReader"
        Effect    = "Deny"
        Principal = "*"
        Action    = ["s3:PutObject*", "s3:DeleteObject*", "s3:AbortMultipartUpload"]
        Resource  = "${local.bucket_arn}/*"
        Condition = { ArnLike = { "aws:PrincipalArn" = local.reader_role_arn } }
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
    ]
  })

  assume_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { AWS = var.trusted_principals }
        Action    = "sts:AssumeRole"
      },
    ]
  })

  list_store_prefix = {
    Sid       = "ListTheStorePrefix"
    Effect    = "Allow"
    Action    = "s3:ListBucket"
    Resource  = local.bucket_arn
    Condition = { StringLike = { "s3:prefix" = [var.prefix, "${var.prefix}/*"] } }
  }

  reader_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      local.list_store_prefix,
      { Sid = "ReadTheStore", Effect = "Allow", Action = "s3:GetObject", Resource = local.objects_arn },
      { Sid = "DecryptTheStore", Effect = "Allow", Action = "kms:Decrypt", Resource = local.kms_key_arn },
    ]
  })

  writer_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      local.list_store_prefix,
      {
        Sid      = "ReadAndWriteTheStore"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = local.objects_arn
      },
      {
        Sid      = "EncryptAndDecryptTheStore"
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource = local.kms_key_arn
      },
    ]
  })
}
