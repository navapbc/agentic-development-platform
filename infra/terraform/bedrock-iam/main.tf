locals {
  enabled = var.create_invoke_policy
}

data "aws_iam_policy_document" "invoke" {
  count = local.enabled ? 1 : 0

  statement {
    sid    = "InvokeBedrockModels"
    effect = "Allow"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]
    resources = var.model_resources
  }

  dynamic "statement" {
    for_each = var.include_discovery ? [1] : []
    content {
      sid    = "DiscoverBedrockModels"
      effect = "Allow"
      actions = [
        "bedrock:ListFoundationModels",
        "bedrock:GetFoundationModel",
        "bedrock:ListInferenceProfiles",
        "bedrock:GetInferenceProfile",
      ]
      resources = ["*"]
    }
  }
}

resource "aws_iam_policy" "invoke" {
  count = local.enabled ? 1 : 0

  name_prefix = "${var.name_prefix}-bedrock-invoke-"
  description = "Grants bedrock:InvokeModel for platform workloads reaching Bedrock over the interface endpoint."
  policy      = data.aws_iam_policy_document.invoke[0].json

  tags = var.tags
}
