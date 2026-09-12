data "aws_iam_policy_document" "ecs_exec_kms_policy" {
  count = local.enabled ? 1 : 0

  statement {
    sid       = "EnableAccountKeyAdministration"
    actions   = ["kms:*"]
    resources = ["*"]

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
  }
}

resource "aws_kms_key" "ecs_exec" {
  count = local.enabled ? 1 : 0

  description         = "Encrypts the ECS Exec data channel for ${local.task_label}."
  policy              = data.aws_iam_policy_document.ecs_exec_kms_policy[0].json
  enable_key_rotation = true

  tags = merge(var.tags, { Name = "${var.name_prefix}-jumphost-exec-kms" })
}

resource "aws_kms_alias" "ecs_exec" {
  count = local.enabled ? 1 : 0

  name          = "alias/${local.task_label}-ecs-exec"
  target_key_id = aws_kms_key.ecs_exec[0].id
}

data "aws_secretsmanager_secret" "injected" {
  for_each = local.enabled ? { for s in var.secrets : s.name => s.secret_arn } : {}
  arn      = each.value
}

resource "aws_kms_grant" "secrets_decrypt" {
  for_each = {
    for name, secret in data.aws_secretsmanager_secret.injected :
    name => secret if secret.kms_key_id != ""
  }

  name              = "${local.task_label}-secret-${each.key}"
  key_id            = each.value.kms_key_id
  grantee_principal = aws_iam_role.execution[0].arn
  operations        = ["Decrypt"]

  constraints {
    encryption_context_subset = {
      SecretARN = each.value.arn
    }
  }
}
