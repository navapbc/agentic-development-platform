locals {
  ecs_arn_prefix = "arn:${local.partition}:ecs:${local.region}:${local.account_id}"
  cluster_condition = {
    ArnEquals = { "ecs:cluster" = local.cluster_arn }
  }

  deployer_statements = concat(
    [
      {
        Sid      = "DescribeTheCluster"
        Effect   = "Allow"
        Action   = "ecs:DescribeClusters"
        Resource = local.cluster_arn
      },
      {
        Sid    = "ManageServicesInTheCluster"
        Effect = "Allow"
        Action = [
          "ecs:CreateService",
          "ecs:UpdateService",
          "ecs:DeleteService",
          "ecs:DescribeServices",
          "ecs:ListServiceDeployments",
          "ecs:DescribeServiceDeployments",
          "ecs:DescribeServiceRevisions",
        ]
        Resource = [
          "${local.ecs_arn_prefix}:service/${local.cluster_name}/*",
          "${local.ecs_arn_prefix}:service-deployment/${local.cluster_name}/*",
          "${local.ecs_arn_prefix}:service-revision/${local.cluster_name}/*",
        ]
      },
      {
        Sid       = "RunTasksInTheCluster"
        Effect    = "Allow"
        Action    = "ecs:RunTask"
        Resource  = "${local.ecs_arn_prefix}:task-definition/*"
        Condition = local.cluster_condition
      },
      {
        Sid      = "ManageTasksInTheCluster"
        Effect   = "Allow"
        Action   = ["ecs:DescribeTasks", "ecs:StopTask"]
        Resource = "${local.ecs_arn_prefix}:task/${local.cluster_name}/*"
      },
      {
        Sid       = "ListWithinTheCluster"
        Effect    = "Allow"
        Action    = ["ecs:ListServices", "ecs:ListTasks"]
        Resource  = "*"
        Condition = local.cluster_condition
      },
      {
        Sid      = "RegisterFargateTaskDefinitions"
        Effect   = "Allow"
        Action   = "ecs:RegisterTaskDefinition"
        Resource = "${local.ecs_arn_prefix}:task-definition/*"
        Condition = {
          "ForAllValues:StringEquals" = { "ecs:compute-compatibility" = ["FARGATE"] }
          Null                        = { "ecs:compute-compatibility" = "false" }
        }
      },
      {
        Sid    = "ReadAndRetireTaskDefinitions"
        Effect = "Allow"
        Action = [
          "ecs:DescribeTaskDefinition",
          "ecs:DeregisterTaskDefinition",
          "ecs:ListTaskDefinitions",
        ]
        Resource = "*"
      },
      {
        Sid    = "TagOnCreate"
        Effect = "Allow"
        Action = "ecs:TagResource"
        Resource = [
          "${local.ecs_arn_prefix}:service/${local.cluster_name}/*",
          "${local.ecs_arn_prefix}:task/${local.cluster_name}/*",
          "${local.ecs_arn_prefix}:task-definition/*",
        ]
        Condition = {
          StringEquals = { "ecs:CreateAction" = ["CreateService", "RunTask", "RegisterTaskDefinition"] }
        }
      },
    ],
    length(var.deployer_passable_role_arns) == 0 ? [] : [
      {
        Sid      = "PassTaskRolesToEcs"
        Effect   = "Allow"
        Action   = "iam:PassRole"
        Resource = var.deployer_passable_role_arns
        Condition = {
          StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" }
        }
      },
    ],
  )

  deployer_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = local.deployer_statements
  })

  exec_task_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "OpenExecSessionChannels"
        Effect = "Allow"
        Action = [
          "ssmmessages:CreateControlChannel",
          "ssmmessages:CreateDataChannel",
          "ssmmessages:OpenControlChannel",
          "ssmmessages:OpenDataChannel",
        ]
        Resource = "*"
      },
      {
        Sid      = "DecryptExecSessions"
        Effect   = "Allow"
        Action   = "kms:Decrypt"
        Resource = local.enabled ? aws_kms_key.logs[0].arn : ""
      },
      {
        Sid      = "FindTheExecLogGroup"
        Effect   = "Allow"
        Action   = "logs:DescribeLogGroups"
        Resource = "*"
      },
      {
        Sid      = "WriteExecSessionLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:DescribeLogStreams", "logs:PutLogEvents"]
        Resource = "arn:${local.partition}:logs:${local.region}:${local.account_id}:log-group:${local.exec_log_group_name}:*"
      },
    ]
  })
}

resource "aws_iam_policy" "deployer" {
  count = local.enabled ? 1 : 0

  name        = "${local.cluster_name}-deployer"
  description = "Registers Fargate task definitions and manages services and tasks in the ${local.cluster_name} ECS cluster only."
  policy      = local.deployer_policy

  tags = var.tags
}

resource "aws_iam_policy" "exec_task" {
  count = local.enabled ? 1 : 0

  name        = "${local.cluster_name}-exec-task"
  description = "Lets a task in the ${local.cluster_name} ECS cluster accept ECS Exec sessions and log them to the encrypted Exec log group."
  policy      = local.exec_task_policy

  tags = var.tags
}
