data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

data "aws_region" "current" {}

locals {
  enabled    = var.create_eks_deployer
  deploy     = local.enabled && var.image != ""
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition
  region     = data.aws_region.current.region

  family         = "${var.name_prefix}-eks-deployer"
  container      = "eks-deployer"
  log_group_name = "/aws/ecs/${var.name_prefix}-eks-deployer"
  log_configuration = {
    logDriver = "awslogs"
    options = {
      awslogs-group         = local.log_group_name
      awslogs-region        = local.region
      awslogs-stream-prefix = local.container
    }
  }

  task_trust = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        ArnLike      = { "aws:SourceArn" = "arn:${local.partition}:ecs:${local.region}:${local.account_id}:*" }
        StringEquals = { "aws:SourceAccount" = local.account_id }
      }
    }]
  })
}

resource "aws_kms_key" "logs" {
  count = local.enabled ? 1 : 0

  description         = "Encrypts the eks-deployer task log group."
  enable_key_rotation = true
  policy = jsonencode({
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
        Sid       = "AllowCloudWatchLogsForTheTaskLogGroup"
        Effect    = "Allow"
        Principal = { Service = "logs.${local.region}.amazonaws.com" }
        Action    = ["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"]
        Resource  = "*"
        Condition = {
          ArnEquals = {
            "kms:EncryptionContext:aws:logs:arn" = "arn:${local.partition}:logs:${local.region}:${local.account_id}:log-group:${local.log_group_name}"
          }
        }
      },
    ]
  })

  tags = merge(var.tags, { Name = "${var.name_prefix}-eks-deployer-logs" })
}

resource "aws_kms_alias" "logs" {
  count = local.enabled ? 1 : 0

  name          = "alias/${var.name_prefix}-eks-deployer-logs"
  target_key_id = aws_kms_key.logs[0].id
}

resource "aws_cloudwatch_log_group" "task" {
  count = local.enabled ? 1 : 0

  name              = local.log_group_name
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.logs[0].arn

  tags = var.tags
}

resource "aws_iam_role" "execution" {
  count = local.enabled ? 1 : 0

  name_prefix        = "${var.name_prefix}-eks-deployer-exec-"
  description        = "Pulls the eks-deployer image and writes its logs."
  assume_role_policy = local.task_trust

  tags = var.tags
}

resource "aws_iam_role_policy" "execution" {
  count = local.enabled ? 1 : 0

  name = "execution"
  role = aws_iam_role.execution[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "AuthenticateToEcr"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Sid      = "PullTheImage"
        Effect   = "Allow"
        Action   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"]
        Resource = var.repository_arn
      },
      {
        Sid      = "WriteTheLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.task[0].arn}:*"
      },
    ]
  })
}

# The task signs its Kubernetes API token with this role, which the cluster maps to an access
# entry. The role carries no AWS permission of its own.
resource "aws_iam_role" "task" {
  count = local.enabled ? 1 : 0

  name_prefix        = "${var.name_prefix}-eks-deployer-"
  description        = "Identity the eks-deployer task presents to the EKS API through its access entry."
  assume_role_policy = local.task_trust

  tags = var.tags
}

data "aws_ec2_managed_prefix_list" "s3" {
  count = local.enabled ? 1 : 0

  name = "com.amazonaws.${local.region}.s3"
}

resource "aws_security_group" "task" {
  count = local.enabled ? 1 : 0

  name_prefix = "${var.name_prefix}-eks-deployer-"
  description = "eks-deployer task. No ingress. Egress to the VPC endpoints, S3, and the EKS API, which the cluster module adds."
  vpc_id      = var.vpc_id

  tags = merge(var.tags, { Name = "${var.name_prefix}-eks-deployer" })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_egress_rule" "endpoints" {
  count = local.enabled ? 1 : 0

  security_group_id            = aws_security_group.task[0].id
  description                  = "HTTPS to the VPC interface endpoints for ECR and CloudWatch Logs"
  from_port                    = 443
  to_port                      = 443
  ip_protocol                  = "tcp"
  referenced_security_group_id = var.endpoints_security_group_id
}

resource "aws_vpc_security_group_egress_rule" "s3" {
  count = local.enabled ? 1 : 0

  security_group_id = aws_security_group.task[0].id
  description       = "HTTPS to S3 for ECR image layers through the gateway endpoint"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
  prefix_list_id    = data.aws_ec2_managed_prefix_list.s3[0].id
}

resource "aws_ecs_task_definition" "deployer" {
  count = local.deploy ? 1 : 0

  family                   = local.family
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = aws_iam_role.execution[0].arn
  task_role_arn            = aws_iam_role.task[0].arn

  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }

  volume {
    name = "tmp"
  }

  # The task volume is created owned by root, and the deployer runs as nobody.
  container_definitions = jsonencode([
    {
      name                   = "volume-permissions"
      image                  = var.image
      essential              = false
      user                   = "0"
      readonlyRootFilesystem = true
      entryPoint             = ["/bin/chmod"]
      command                = ["1777", "/tmp"]
      mountPoints            = [{ sourceVolume = "tmp", containerPath = "/tmp" }]
      logConfiguration       = local.log_configuration
    },
    {
      name                   = local.container
      image                  = var.image
      essential              = true
      readonlyRootFilesystem = true
      dependsOn              = [{ containerName = "volume-permissions", condition = "SUCCESS" }]
      environment            = [{ name = "DEPLOY_INPUTS", value = var.deploy_inputs }]
      mountPoints            = [{ sourceVolume = "tmp", containerPath = "/tmp" }]
      logConfiguration       = local.log_configuration
    },
  ])

  tags = var.tags
}
