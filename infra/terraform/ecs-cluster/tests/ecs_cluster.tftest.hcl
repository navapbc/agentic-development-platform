mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "111111111111" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_data "aws_region" {
    defaults = { region = "us-east-1" }
  }
}

override_resource {
  target          = aws_kms_key.logs
  override_during = plan
  values          = { arn = "arn:aws:kms:us-east-1:111111111111:key/logs" }
}

override_resource {
  target          = aws_kms_key.storage
  override_during = plan
  values          = { arn = "arn:aws:kms:us-east-1:111111111111:key/storage" }
}

variables {
  name_prefix = "agentic-platform"
}

run "disabled_module_creates_nothing" {
  command = plan

  assert {
    condition = (
      length(aws_ecs_cluster.this) == 0 && length(aws_kms_key.logs) == 0 && length(aws_kms_key.storage) == 0 &&
      length(aws_cloudwatch_log_group.exec) == 0 && length(aws_cloudwatch_log_group.insights) == 0 &&
      length(aws_iam_policy.deployer) == 0 && length(aws_iam_policy.exec_task) == 0
    )
    error_message = "The disabled module planned resources."
  }
}

run "cluster_encrypts_exec_and_fargate_storage" {
  command = plan

  variables {
    create_ecs_cluster = true
  }

  assert {
    condition     = aws_ecs_cluster.this[0].name == "agentic-platform-ecs"
    error_message = "The cluster is not named <name_prefix>-ecs."
  }

  assert {
    condition     = one([for s in aws_ecs_cluster.this[0].setting : s.value if s.name == "containerInsights"]) == "enhanced"
    error_message = "Container Insights is not at the enhanced level."
  }

  assert {
    condition = (
      aws_ecs_cluster.this[0].configuration[0].execute_command_configuration[0].kms_key_id == "arn:aws:kms:us-east-1:111111111111:key/logs" &&
      aws_ecs_cluster.this[0].configuration[0].execute_command_configuration[0].logging == "OVERRIDE" &&
      aws_ecs_cluster.this[0].configuration[0].execute_command_configuration[0].log_configuration[0].cloud_watch_encryption_enabled
    )
    error_message = "ECS Exec sessions are not encrypted under the logs key and logged to an encrypted group."
  }

  assert {
    condition     = aws_ecs_cluster.this[0].configuration[0].managed_storage_configuration[0].fargate_ephemeral_storage_kms_key_id == "arn:aws:kms:us-east-1:111111111111:key/storage"
    error_message = "Fargate ephemeral storage is not encrypted under the storage key."
  }

  assert {
    condition = (
      aws_cloudwatch_log_group.exec[0].kms_key_id == "arn:aws:kms:us-east-1:111111111111:key/logs" &&
      aws_cloudwatch_log_group.insights[0].kms_key_id == "arn:aws:kms:us-east-1:111111111111:key/logs" &&
      aws_cloudwatch_log_group.insights[0].name == "/aws/ecs/containerinsights/agentic-platform-ecs/performance"
    )
    error_message = "The Exec and Container Insights log groups are not encrypted under the logs key."
  }

  assert {
    condition     = aws_kms_key.logs[0].enable_key_rotation && aws_kms_key.storage[0].enable_key_rotation
    error_message = "A cluster key does not rotate."
  }
}

run "capacity_providers_default_to_fargate" {
  command = plan

  variables {
    create_ecs_cluster = true
  }

  assert {
    condition     = toset(aws_ecs_cluster_capacity_providers.this[0].capacity_providers) == toset(["FARGATE", "FARGATE_SPOT"])
    error_message = "The cluster does not offer exactly FARGATE and FARGATE_SPOT."
  }

  assert {
    condition     = [for s in aws_ecs_cluster_capacity_providers.this[0].default_capacity_provider_strategy : s.capacity_provider] == ["FARGATE"]
    error_message = "The default strategy is not FARGATE."
  }
}

run "storage_key_admits_fargate_for_this_cluster_only" {
  command = plan

  variables {
    create_ecs_cluster = true
  }

  assert {
    condition = alltrue([
      for s in jsondecode(local.storage_key_policy).Statement :
      s.Condition.StringEquals["kms:EncryptionContext:aws:ecs:clusterName"] == "agentic-platform-ecs" &&
      s.Condition.StringEquals["kms:EncryptionContext:aws:ecs:clusterAccount"] == "111111111111"
      if try(s.Principal.Service, "") == "fargate.amazonaws.com" && s.Action != "kms:DescribeKey"
    ])
    error_message = "A Fargate key grant is not bound to this cluster's encryption context."
  }

  assert {
    condition = one([
      for s in jsondecode(local.storage_key_policy).Statement :
      s.Condition["ForAllValues:StringEquals"]["kms:GrantOperations"] if s.Action == "kms:CreateGrant"
    ]) == ["Decrypt"]
    error_message = "Fargate may create grants for operations other than Decrypt."
  }

  assert {
    condition = one([
      for s in jsondecode(local.logs_key_policy).Statement :
      s.Condition.ArnEquals["kms:EncryptionContext:aws:logs:arn"] if try(s.Principal.Service, "") == "logs.us-east-1.amazonaws.com"
      ]) == [
      "arn:aws:logs:us-east-1:111111111111:log-group:/aws/ecs/agentic-platform-ecs/exec",
      "arn:aws:logs:us-east-1:111111111111:log-group:/aws/ecs/containerinsights/agentic-platform-ecs/performance",
    ]
    error_message = "CloudWatch Logs may use the logs key for groups other than the cluster's."
  }
}

run "deployer_is_confined_to_the_cluster" {
  command = plan

  variables {
    create_ecs_cluster = true
  }

  assert {
    condition = alltrue(flatten([
      for s in jsondecode(local.deployer_policy).Statement : [
        for r in flatten([s.Resource]) :
        r == "*" || r == "arn:aws:ecs:us-east-1:111111111111:cluster/agentic-platform-ecs" ||
        startswith(r, "arn:aws:ecs:us-east-1:111111111111:task-definition/") ||
        can(regex("^arn:aws:ecs:us-east-1:111111111111:(service|service-deployment|service-revision|task)/agentic-platform-ecs/", r))
      ]
    ]))
    error_message = "A deployer statement names an ECS resource outside the cluster."
  }

  assert {
    condition = alltrue([
      for s in jsondecode(local.deployer_policy).Statement :
      s.Condition.ArnEquals["ecs:cluster"] == "arn:aws:ecs:us-east-1:111111111111:cluster/agentic-platform-ecs"
      if contains(["RunTasksInTheCluster", "ListWithinTheCluster"], s.Sid)
    ])
    error_message = "RunTask or the list actions are not conditioned on this cluster."
  }

  assert {
    condition = toset(flatten([
      for s in jsondecode(local.deployer_policy).Statement : flatten([s.Action]) if s.Resource == "*"
    ])) == toset(["ecs:ListServices", "ecs:ListTasks", "ecs:DescribeTaskDefinition", "ecs:DeregisterTaskDefinition", "ecs:ListTaskDefinitions"])
    error_message = "The deployer holds an unscoped action beyond the list and task definition reads."
  }

  assert {
    condition     = length([for s in jsondecode(local.deployer_policy).Statement : s if s.Action == "iam:PassRole"]) == 0
    error_message = "The deployer may pass roles although no passable role is listed."
  }
}

run "deployer_passes_only_listed_roles_to_ecs_tasks" {
  command = plan

  variables {
    create_ecs_cluster          = true
    deployer_passable_role_arns = ["arn:aws:iam::111111111111:role/team-a-*"]
  }

  assert {
    condition = one([
      for s in jsondecode(local.deployer_policy).Statement : s if s.Action == "iam:PassRole"
      ]) == {
      Sid       = "PassTaskRolesToEcs"
      Effect    = "Allow"
      Action    = "iam:PassRole"
      Resource  = ["arn:aws:iam::111111111111:role/team-a-*"]
      Condition = { StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" } }
    }
    error_message = "PassRole is not limited to the listed roles and to ECS tasks."
  }
}

run "exec_task_policy_writes_only_the_exec_log_group" {
  command = plan

  variables {
    create_ecs_cluster = true
  }

  assert {
    condition = one([
      for s in jsondecode(local.exec_task_policy).Statement : s.Resource if s.Sid == "WriteExecSessionLogs"
    ]) == "arn:aws:logs:us-east-1:111111111111:log-group:/aws/ecs/agentic-platform-ecs/exec:*"
    error_message = "The exec task policy writes log groups other than the Exec group."
  }

  assert {
    condition     = one([for s in jsondecode(local.exec_task_policy).Statement : s.Resource if s.Sid == "DecryptExecSessions"]) == "arn:aws:kms:us-east-1:111111111111:key/logs"
    error_message = "The exec task policy decrypts with a key other than the logs key."
  }
}
