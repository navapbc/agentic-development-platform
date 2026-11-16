mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "111111111111" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_data "aws_ec2_managed_prefix_list" {
    defaults = { id = "pl-63a5400a" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  mock_data "aws_secretsmanager_secret" {
    defaults = { kms_key_id = "" }
  }
}

override_resource {
  target          = aws_cloudwatch_log_group.exec
  override_during = plan
  values          = { arn = "arn:aws:logs:us-east-1:111111111111:log-group:/aws/ecs/fargate/agentic-platform-bedrock-jumphost" }
}

override_resource {
  target          = aws_kms_key.ecs_exec
  override_during = plan
  values          = { arn = "arn:aws:kms:us-east-1:111111111111:key/exec" }
}

override_resource {
  target          = aws_s3_bucket.exec_logs
  override_during = plan
  values = {
    id  = "agentic-platform-bedrock-jumphost-exec-1"
    arn = "arn:aws:s3:::agentic-platform-bedrock-jumphost-exec-1"
  }
}

variables {
  create_jumphost             = true
  vpc_id                      = "vpc-1"
  subnet_ids                  = ["subnet-a"]
  endpoints_security_group_id = "sg-endpoints"
  image_uri                   = "111111111111.dkr.ecr.us-east-1.amazonaws.com/agentic-platform/jumphost-base:v1"
  ecr_repository_arn          = "arn:aws:ecr:us-east-1:111111111111:repository/agentic-platform/jumphost-base"
  access_log_bucket           = "agentic-platform-access-logs"
}

run "only_actions_without_resource_level_scoping_take_every_resource" {
  command = plan

  assert {
    condition = {
      for statement in concat(data.aws_iam_policy_document.execution[0].statement, data.aws_iam_policy_document.task[0].statement) :
      statement.sid => toset(statement.actions) if contains(statement.resources, "*")
      } == {
      EcrAuth                   = toset(["ecr:GetAuthorizationToken"])
      EcsExecChannel            = toset(["ssmmessages:CreateControlChannel", "ssmmessages:CreateDataChannel", "ssmmessages:OpenControlChannel"])
      ExecLoggingDescribeGroups = toset(["logs:DescribeLogGroups"])
    }
    error_message = "A statement grants every resource for an action that supports resource-level scoping."
  }
}

run "scoped_statements_name_only_the_pull_the_forward_and_the_log_write" {
  command = plan

  assert {
    condition = {
      for statement in data.aws_iam_policy_document.execution[0].statement :
      statement.sid => toset(statement.resources) if statement.sid != "EcrAuth"
      } == {
      EcrPull = toset(["arn:aws:ecr:us-east-1:111111111111:repository/agentic-platform/jumphost-base"])
      Logs    = toset(["arn:aws:logs:us-east-1:111111111111:log-group:/aws/ecs/fargate/agentic-platform-bedrock-jumphost:log-stream:ecs/*"])
    }
    error_message = "The execution role reaches beyond the image repository and the container log streams."
  }

  assert {
    condition = {
      for statement in data.aws_iam_policy_document.task[0].statement :
      statement.sid => toset(statement.resources) if !contains(["EcsExecChannel", "ExecLoggingDescribeGroups"], statement.sid)
      } == {
      EcsExecDataChannel = toset(["arn:aws:ssm:us-east-1:111111111111:session/*"])
      DecryptExecChannel = toset(["arn:aws:kms:us-east-1:111111111111:key/exec"])
      ExecLoggingCloudWatch = toset([
        "arn:aws:logs:us-east-1:111111111111:log-group:/aws/ecs/fargate/agentic-platform-bedrock-jumphost",
        "arn:aws:logs:us-east-1:111111111111:log-group:/aws/ecs/fargate/agentic-platform-bedrock-jumphost:log-stream:*",
      ])
      ExecLoggingS3Put    = toset(["arn:aws:s3:::agentic-platform-bedrock-jumphost-exec-1/exec-output/*"])
      ExecLoggingS3Bucket = toset(["arn:aws:s3:::agentic-platform-bedrock-jumphost-exec-1"])
    }
    error_message = "The task role reaches beyond the exec sessions, the exec key, and the exec log destinations."
  }

  assert {
    condition     = !contains([for statement in data.aws_iam_policy_document.task[0].statement : statement.sid], "ExecLoggingS3Encrypt")
    error_message = "The task role holds a log bucket key grant while the bucket uses SSE-S3."
  }

  assert {
    condition = (
      aws_ecs_cluster.this[0].configuration[0].execute_command_configuration[0].log_configuration[0].s3_key_prefix == "exec-output" &&
      jsondecode(aws_ecs_task_definition.this[0].container_definitions)[0].logConfiguration.options["awslogs-stream-prefix"] == "ecs"
    )
    error_message = "The exec log key prefix or the container log stream prefix differs from the one the roles may write."
  }
}

run "a_customer_managed_log_bucket_key_admits_only_the_upload_on_that_key" {
  command = plan

  variables {
    logs_bucket_kms_key_arn = "arn:aws:kms:us-east-1:111111111111:key/exec-logs"
  }

  assert {
    condition = [
      for statement in data.aws_iam_policy_document.task[0].statement :
      { actions = toset(statement.actions), resources = toset(statement.resources) } if statement.sid == "ExecLoggingS3Encrypt"
      ] == [{
        actions   = toset(["kms:GenerateDataKey", "kms:Decrypt"])
        resources = toset(["arn:aws:kms:us-east-1:111111111111:key/exec-logs"])
    }]
    error_message = "The task role cannot encrypt the session log upload under the bucket key, or reaches another key."
  }
}

run "the_secrets_statement_names_only_the_injected_secrets" {
  command = plan

  variables {
    secrets = [{ name = "TOKEN", secret_arn = "arn:aws:secretsmanager:us-east-1:111111111111:secret:jumphost-token" }]
  }

  assert {
    condition = [
      for statement in data.aws_iam_policy_document.execution[0].statement :
      toset(statement.resources) if statement.sid == "Secrets"
    ] == [toset(["arn:aws:secretsmanager:us-east-1:111111111111:secret:jumphost-token"])]
    error_message = "The Secrets statement reaches beyond the injected secret."
  }
}
