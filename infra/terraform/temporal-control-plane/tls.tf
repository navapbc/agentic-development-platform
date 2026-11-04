data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

data "aws_region" "current" {}

locals {
  enabled                  = var.create_temporal_control_plane
  frontend_from_secrets    = var.frontend_acm_certificate_arn == ""
  tracked_certificate_arns = compact([var.frontend_acm_certificate_arn, var.internode_acm_certificate_arn])
  track_expiry             = local.enabled && length(local.tracked_certificate_arns) > 0

  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition
  region     = data.aws_region.current.region

  # Clients verify the frontend against the CA secret unless an ACM frontend chains to a public root.
  client_ca = local.enabled && (local.frontend_from_secrets || !var.frontend_certificate_publicly_trusted)
  # Only the server reads the internode and frontend keys. Every task reads the CA.
  tls_readers = {
    for key in keys(local.tls_secrets) : key => distinct(concat(
      var.tls_secret_readers,
      [for name, role in aws_iam_role.execution : role.arn if key == "ca" || name == "server"],
    ))
  }

  tls_secrets = local.enabled ? merge(
    {
      ca        = "PEM bundle of the CA that issues the internode certificate, and the frontend certificate when it comes from Secrets Manager."
      internode = "JSON with the PEM certificate and private_key the Temporal services present to each other over mutual TLS."
    },
    local.frontend_from_secrets ? {
      frontend = "JSON with the PEM certificate and private_key the Temporal frontend presents to clients."
    } : {},
  ) : {}

  secret_policies = {
    for key, readers in local.tls_readers : key => jsonencode({
      Version = "2012-10-17"
      Statement = concat(
        length(readers) == 0 ? [] : [
          {
            Sid       = "AllowReaders"
            Effect    = "Allow"
            Principal = { AWS = readers }
            Action    = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
            Resource  = "*"
          },
        ],
        [
          merge(
            {
              Sid       = "DenyOtherReaders"
              Effect    = "Deny"
              Principal = { AWS = "*" }
              Action    = "secretsmanager:GetSecretValue"
              Resource  = "*"
            },
            length(readers) == 0 ? {} : {
              Condition = { ArnNotEquals = { "aws:PrincipalArn" = readers } }
            },
          ),
        ],
      )
    })
  }

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
        Sid       = "AllowCloudWatchLogsForTheTaskLogGroups"
        Effect    = "Allow"
        Principal = { Service = "logs.${local.region}.amazonaws.com" }
        Action    = ["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"]
        Resource  = "*"
        Condition = {
          ArnLike = { "kms:EncryptionContext:aws:logs:arn" = "arn:${local.partition}:logs:${local.region}:${local.account_id}:log-group:/aws/ecs/${var.name_prefix}-temporal/*" }
        }
      },
      {
        Sid       = "AllowEventBridgeToPublishExpiryNotices"
        Effect    = "Allow"
        Principal = { Service = "events.amazonaws.com" }
        Action    = ["kms:GenerateDataKey*", "kms:Decrypt"]
        Resource  = "*"
        Condition = {
          ArnEquals = { "aws:SourceArn" = "arn:${local.partition}:events:${local.region}:${local.account_id}:rule/${var.name_prefix}-temporal-certificate-expiry" }
        }
      },
    ]
  })
}

resource "aws_kms_key" "temporal" {
  count = local.enabled ? 1 : 0

  description         = "Encrypts the ${var.name_prefix} Temporal TLS secrets and the certificate expiry topic."
  policy              = local.key_policy
  enable_key_rotation = true

  tags = merge(var.tags, { Name = "${var.name_prefix}-temporal" })
}

resource "aws_kms_alias" "temporal" {
  count = local.enabled ? 1 : 0

  name          = "alias/${var.name_prefix}-temporal"
  target_key_id = aws_kms_key.temporal[0].id
}

resource "aws_secretsmanager_secret" "tls" {
  for_each = local.tls_secrets

  name                    = "${var.name_prefix}/temporal/tls-${each.key}"
  description             = each.value
  kms_key_id              = aws_kms_key.temporal[0].arn
  recovery_window_in_days = var.secret_recovery_window_days

  tags = merge(var.tags, { SecretGroup = "temporal-tls" })
}

resource "aws_secretsmanager_secret_policy" "tls" {
  for_each = local.tls_secrets

  secret_arn          = aws_secretsmanager_secret.tls[each.key].arn
  policy              = local.secret_policies[each.key]
  block_public_policy = true
}

resource "aws_sns_topic" "certificate_expiry" {
  count = local.track_expiry ? 1 : 0

  name              = "${var.name_prefix}-temporal-certificate-expiry"
  kms_master_key_id = aws_kms_key.temporal[0].arn

  tags = var.tags
}

resource "aws_sns_topic_policy" "certificate_expiry" {
  count = local.track_expiry ? 1 : 0

  arn = aws_sns_topic.certificate_expiry[0].arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowTheExpiryRule"
        Effect    = "Allow"
        Principal = { Service = "events.amazonaws.com" }
        Action    = "sns:Publish"
        Resource  = aws_sns_topic.certificate_expiry[0].arn
        Condition = { ArnEquals = { "aws:SourceArn" = aws_cloudwatch_event_rule.certificate_expiry[0].arn } }
      },
    ]
  })
}

resource "aws_cloudwatch_event_rule" "certificate_expiry" {
  count = local.track_expiry ? 1 : 0

  name        = "${var.name_prefix}-temporal-certificate-expiry"
  description = "Notifies when a tracked Temporal certificate in ACM nears expiry or expires."
  event_pattern = jsonencode({
    source        = ["aws.acm"]
    "detail-type" = ["ACM Certificate Approaching Expiration", "ACM Certificate Expired"]
    resources     = local.tracked_certificate_arns
  })

  tags = var.tags
}

resource "aws_cloudwatch_event_target" "certificate_expiry" {
  count = local.track_expiry ? 1 : 0

  rule = aws_cloudwatch_event_rule.certificate_expiry[0].name
  arn  = aws_sns_topic.certificate_expiry[0].arn
}
