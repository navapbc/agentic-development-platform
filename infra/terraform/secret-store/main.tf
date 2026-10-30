data "aws_caller_identity" "current" {
  count = local.enabled ? 1 : 0
}

data "aws_partition" "current" {
  count = local.enabled ? 1 : 0
}

locals {
  enabled          = var.create_secret_store
  database_enabled = local.enabled && var.create_database_credentials
  cache_enabled    = local.enabled && var.rotate_elasticache_user
  rotation_enabled = local.database_enabled || local.cache_enabled

  account_id = try(data.aws_caller_identity.current[0].account_id, "")
  partition  = try(data.aws_partition.current[0].partition, "aws")

  generated_secrets = {
    litellm_master_key = {
      description = "LiteLLM proxy master key, set as LITELLM_MASTER_KEY and REBAR_LLM_API_KEY."
      prefix      = "sk-"
      hex         = false
    }
    review_attestation_secret = {
      description = "HMAC key for unsigned review verdict attestations, set as REVIEW_ATTESTATION_SECRET."
      prefix      = ""
      hex         = false
    }
    langfuse_nextauth_secret = {
      description = "Langfuse session signing secret, set as NEXTAUTH_SECRET."
      prefix      = ""
      hex         = false
    }
    langfuse_salt = {
      description = "Langfuse API key hashing salt, set as SALT."
      prefix      = ""
      hex         = false
    }
    langfuse_encryption_key = {
      description = "Langfuse data encryption key, 64 hex characters, set as ENCRYPTION_KEY."
      prefix      = ""
      hex         = true
    }
    langfuse_public_key = {
      description = "Langfuse project public key, set as LANGFUSE_PUBLIC_KEY and LANGFUSE_INIT_PROJECT_PUBLIC_KEY."
      prefix      = "pk-lf-"
      hex         = false
    }
    langfuse_secret_key = {
      description = "Langfuse project secret key, set as LANGFUSE_SECRET_KEY and LANGFUSE_INIT_PROJECT_SECRET_KEY."
      prefix      = "sk-lf-"
      hex         = false
    }
    clickhouse_password = {
      description = "ClickHouse user password, set as CLICKHOUSE_PASSWORD."
      prefix      = ""
      hex         = false
    }
    coordination_shared_secret = {
      description = "Bearer token for the coordination RPC, set as COORDINATION_SHARED_SECRET on the coordination service and on every worker."
      prefix      = ""
      hex         = false
    }
  }

  external_secrets = {
    github_token           = "GitHub token for the source control integration, set as GITHUB_TOKEN."
    github_webhook_secret  = "GitHub webhook signing secret, set as GITHUB_WEBHOOK_SECRET."
    jira_api_token         = "Jira Cloud API token, set as JIRA_API_TOKEN."
    jira_pat               = "Jira Data Center personal access token, set as JIRA_PAT."
    jira_webhook_secret    = "Jira webhook signing secret, set as JIRA_WEBHOOK_SECRET."
    slack_webhook_url      = "Slack incoming webhook URL for alerts, set as SLACK_WEBHOOK_URL."
    review_signing_key     = "OpenSSH private key that signs review verdicts, set as REVIEW_SIGNING_KEY."
    authorship_signing_key = "OpenSSH private key that signs authored artifacts, set as AUTHORSHIP_SIGNING_KEY."
  }

  database_roles = {
    platform_migrator = "Platform migration role, which owns the platform schema and runs Alembic."
    platform_app      = "Platform application role, with DML on the platform schema and read on alembic_version."
    temporal          = "Temporal server role, which owns the temporal and temporal_visibility databases."
    langfuse          = "Langfuse role, which owns the langfuse database."
  }

  secret_catalog = merge(
    { for key, secret in local.generated_secrets : key => { description = secret.description, group = "generated" } },
    { for key, description in local.external_secrets : key => { description = description, group = "external" } },
    local.database_enabled ? { for role, description in local.database_roles : "database_${role}" => { description = description, group = "database" } } : {},
  )
  secret_keys = local.enabled ? local.secret_catalog : {}

  database_secret_keys = { for role in keys(local.database_roles) : role => "database_${role}" if local.database_enabled }
  managed_secret_arns  = { for key, secret in aws_secretsmanager_secret.this : key => secret.arn }
  secret_arns = merge(
    local.managed_secret_arns,
    local.cache_enabled ? { elasticache_user = var.elasticache_user_secret_arn } : {},
  )

  rotation_role_arns = {
    for key in keys(local.secret_arns) : key => compact([
      contains(values(local.database_secret_keys), key) ? try(aws_iam_role.postgres[0].arn, "") : "",
      key == "elasticache_user" ? try(aws_iam_role.elasticache[0].arn, "") : "",
    ])
  }
  all_reader_arns = distinct(flatten(values(var.secret_readers)))
}

resource "terraform_data" "inputs_guard" {
  lifecycle {
    precondition {
      condition     = length(setsubtract(keys(var.secret_readers), concat(keys(local.secret_catalog), ["elasticache_user"]))) == 0
      error_message = "secret_readers has a key that names no secret. Use the secret keys the README lists."
    }
    precondition {
      condition     = length(setsubtract(keys(var.restart_services), keys(local.secret_arns))) == 0 || !local.enabled
      error_message = "restart_services has a key that names no secret this configuration creates."
    }
    precondition {
      condition     = !local.database_enabled || (var.database_cluster_arn != "" && var.database_master_secret_arn != "")
      error_message = "create_database_credentials requires database_cluster_arn and database_master_secret_arn."
    }
    precondition {
      condition     = !local.cache_enabled || (var.elasticache_user_secret_arn != "" && var.elasticache_user_arn != "")
      error_message = "rotate_elasticache_user requires elasticache_user_secret_arn and elasticache_user_arn."
    }
    precondition {
      condition     = !local.rotation_enabled || (var.vpc_id != "" && length(var.subnet_ids) > 0 && var.endpoints_security_group_id != "")
      error_message = "Rotation requires vpc_id, subnet_ids, and endpoints_security_group_id for the rotation functions."
    }
  }
}

data "aws_iam_policy_document" "key" {
  count = local.enabled ? 1 : 0

  statement {
    sid       = "EnableAccountKeyAdministration"
    actions   = ["kms:*"]
    resources = ["*"]

    principals {
      type        = "AWS"
      identifiers = ["arn:${local.partition}:iam::${local.account_id}:root"]
    }
  }

  dynamic "statement" {
    for_each = length(local.all_reader_arns) == 0 ? [] : [1]
    content {
      sid       = "DecryptForSecretReaders"
      actions   = ["kms:Decrypt"]
      resources = ["*"]

      principals {
        type        = "AWS"
        identifiers = local.all_reader_arns
      }

      condition {
        test     = "StringEquals"
        variable = "kms:ViaService"
        values   = ["secretsmanager.${var.region}.amazonaws.com"]
      }
    }
  }

  statement {
    sid = "EncryptRotationFunctionLogs"
    actions = [
      "kms:Encrypt*",
      "kms:Decrypt*",
      "kms:ReEncrypt*",
      "kms:GenerateDataKey*",
      "kms:Describe*",
    ]
    resources = ["*"]

    principals {
      type        = "Service"
      identifiers = ["logs.${var.region}.amazonaws.com"]
    }

    condition {
      test     = "ArnLike"
      variable = "kms:EncryptionContext:aws:logs:arn"
      values   = ["arn:${local.partition}:logs:${var.region}:${local.account_id}:log-group:/aws/lambda/${var.name_prefix}-*-rotation"]
    }
  }
}

resource "aws_kms_key" "this" {
  count = local.enabled ? 1 : 0

  description             = "Encrypts the ${var.name_prefix} platform secrets and their rotation function logs."
  policy                  = data.aws_iam_policy_document.key[0].json
  enable_key_rotation     = true
  deletion_window_in_days = 30

  tags = merge(var.tags, { Name = "${var.name_prefix}-secrets" })
}

resource "aws_kms_alias" "this" {
  count = local.enabled ? 1 : 0

  name          = "alias/${var.name_prefix}-secrets"
  target_key_id = aws_kms_key.this[0].id
}

resource "aws_secretsmanager_secret" "this" {
  for_each = local.secret_keys

  name                    = "${var.name_prefix}/${each.value.group}/${replace(trimprefix(each.key, "database_"), "_", "-")}"
  description             = each.value.description
  kms_key_id              = aws_kms_key.this[0].arn
  recovery_window_in_days = var.recovery_window_days

  tags = merge(var.tags, { SecretGroup = each.value.group })
}

ephemeral "aws_secretsmanager_random_password" "generated" {
  for_each = local.enabled ? { for key, secret in local.generated_secrets : key => secret if !secret.hex } : {}

  password_length     = 48
  exclude_punctuation = true
}

ephemeral "random_bytes" "generated" {
  for_each = local.enabled ? { for key, secret in local.generated_secrets : key => secret if secret.hex } : {}

  length = 32
}

locals {
  generated_values = merge(
    { for key, value in ephemeral.aws_secretsmanager_random_password.generated : key => "${local.generated_secrets[key].prefix}${value.random_password}" },
    { for key, value in ephemeral.random_bytes.generated : key => value.hex },
  )
}

resource "aws_secretsmanager_secret_version" "generated" {
  for_each = local.enabled ? local.generated_secrets : {}

  secret_id                = aws_secretsmanager_secret.this[each.key].id
  secret_string_wo         = local.generated_values[each.key]
  secret_string_wo_version = lookup(var.generated_secret_versions, each.key, 1)
}

ephemeral "aws_secretsmanager_random_password" "database" {
  for_each = local.database_secret_keys

  password_length     = 40
  exclude_punctuation = true
}

resource "aws_secretsmanager_secret_version" "database" {
  for_each = local.database_secret_keys

  secret_id = aws_secretsmanager_secret.this[each.value].id
  secret_string_wo = jsonencode({
    username = each.key
    password = ephemeral.aws_secretsmanager_random_password.database[each.key].random_password
  })
  secret_string_wo_version = 1
}

data "aws_iam_policy_document" "secret" {
  for_each = local.enabled ? local.secret_arns : {}

  dynamic "statement" {
    for_each = length(lookup(var.secret_readers, each.key, [])) == 0 ? [] : [1]
    content {
      sid       = "AllowReaders"
      actions   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
      resources = ["*"]

      principals {
        type        = "AWS"
        identifiers = var.secret_readers[each.key]
      }
    }
  }

  statement {
    sid       = "DenyOtherReaders"
    effect    = "Deny"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["*"]

    principals {
      type        = "AWS"
      identifiers = ["*"]
    }

    dynamic "condition" {
      for_each = length(concat(lookup(var.secret_readers, each.key, []), local.rotation_role_arns[each.key])) == 0 ? [] : [1]
      content {
        test     = "ArnNotEquals"
        variable = "aws:PrincipalArn"
        values   = concat(lookup(var.secret_readers, each.key, []), local.rotation_role_arns[each.key])
      }
    }
  }
}

resource "aws_secretsmanager_secret_policy" "this" {
  for_each = local.enabled ? local.secret_arns : {}

  secret_arn          = each.value
  policy              = data.aws_iam_policy_document.secret[each.key].json
  block_public_policy = true
}
