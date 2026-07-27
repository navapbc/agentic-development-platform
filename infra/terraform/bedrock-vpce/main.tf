locals {
  enabled        = var.create_bedrock_vpce
  policy_enabled = var.create_bedrock_vpce && var.create_invoke_policy
}

resource "aws_security_group" "bedrock_vpce" {
  count = local.enabled ? 1 : 0

  name_prefix = "${var.name_prefix}-bedrock-vpce-"
  description = "Ingress to the bedrock-runtime interface endpoint (443 from in-VPC subnets only)."
  vpc_id      = var.vpc_id

  ingress {
    description = "HTTPS from allowed in-VPC subnets"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = var.allowed_cidr_blocks
  }

  egress {
    description = "Return traffic"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-bedrock-vpce" })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_endpoint" "bedrock_runtime" {
  count = local.enabled ? 1 : 0

  vpc_id              = var.vpc_id
  service_name        = "com.amazonaws.${var.region}.bedrock-runtime"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = var.subnet_ids
  security_group_ids  = [aws_security_group.bedrock_vpce[0].id]
  private_dns_enabled = var.private_dns_enabled

  tags = merge(var.tags, { Name = "${var.name_prefix}-bedrock-runtime" })
}

data "aws_iam_policy_document" "bedrock_invoke" {
  count = local.policy_enabled ? 1 : 0

  statement {
    sid    = "InvokeBedrockModels"
    effect = "Allow"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_policy" "bedrock_invoke" {
  count = local.policy_enabled ? 1 : 0

  name_prefix = "${var.name_prefix}-bedrock-invoke-"
  description = "Grants bedrock:InvokeModel for platform workloads reaching Bedrock via the interface endpoint."
  policy      = data.aws_iam_policy_document.bedrock_invoke[0].json

  tags = var.tags
}
