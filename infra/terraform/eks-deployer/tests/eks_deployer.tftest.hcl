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
  mock_data "aws_ec2_managed_prefix_list" {
    defaults = { id = "pl-63a5400a" }
  }
}

override_resource {
  target          = aws_kms_key.logs
  override_during = plan
  values          = { arn = "arn:aws:kms:us-east-1:111111111111:key/logs" }
}

override_resource {
  target          = aws_cloudwatch_log_group.task
  override_during = plan
  values          = { arn = "arn:aws:logs:us-east-1:111111111111:log-group:/aws/ecs/agentic-platform-eks-deployer" }
}

variables {
  name_prefix                 = "agentic-platform"
  repository_arn              = "arn:aws:ecr:us-east-1:111111111111:repository/agentic-platform/eks-deployer"
  cluster_arn                 = "arn:aws:ecs:us-east-1:111111111111:cluster/agentic-platform-ecs"
  vpc_id                      = "vpc-1"
  subnet_ids                  = ["subnet-m1", "subnet-m2"]
  endpoints_security_group_id = "sg-endpoints"
  deploy_inputs               = "{\"cluster\":{\"name\":\"agentic-platform-eks\"}}"
}

run "disabled_module_creates_nothing" {
  command = plan

  assert {
    condition = (
      length(aws_ecs_task_definition.deployer) == 0 && length(aws_iam_role.task) == 0 &&
      length(aws_iam_role.execution) == 0 && length(aws_security_group.task) == 0 &&
      length(aws_cloudwatch_log_group.task) == 0 && output.deploy == null
    )
    error_message = "The disabled module planned resources."
  }
}

run "without_the_image_no_task_is_defined" {
  command = plan

  variables {
    create_eks_deployer = true
  }

  assert {
    condition     = length(aws_ecs_task_definition.deployer) == 0 && length(aws_iam_role.task) == 1 && output.deploy == null && output.family == ""
    error_message = "A task definition was planned without an image."
  }
}

run "the_task_runs_the_image_with_the_inputs_and_no_aws_permission" {
  command = plan

  variables {
    create_eks_deployer = true
    image               = "111111111111.dkr.ecr.us-east-1.amazonaws.com/agentic-platform/eks-deployer:0.3.0-abc-arm64@sha256:0000000000000000000000000000000000000000000000000000000000000000"
  }

  assert {
    condition = (
      aws_ecs_task_definition.deployer[0].runtime_platform[0].cpu_architecture == "ARM64" &&
      jsondecode(aws_ecs_task_definition.deployer[0].container_definitions)[1].readonlyRootFilesystem &&
      jsondecode(aws_ecs_task_definition.deployer[0].container_definitions)[1].environment[0].name == "DEPLOY_INPUTS" &&
      jsondecode(aws_ecs_task_definition.deployer[0].container_definitions)[1].environment[0].value == var.deploy_inputs
    )
    error_message = "The deployer does not run the image read-only on ARM64 with DEPLOY_INPUTS."
  }

  assert {
    condition = (
      jsondecode(aws_iam_role_policy.execution[0].policy).Statement[1].Resource == var.repository_arn &&
      length(jsondecode(aws_iam_role_policy.execution[0].policy).Statement) == 3
    )
    error_message = "The execution role pulls beyond its repository or holds more than pull and logs."
  }

  assert {
    condition = (
      aws_vpc_security_group_egress_rule.endpoints[0].referenced_security_group_id == "sg-endpoints" &&
      aws_vpc_security_group_egress_rule.s3[0].prefix_list_id == "pl-63a5400a" &&
      aws_vpc_security_group_egress_rule.endpoints[0].to_port == 443
    )
    error_message = "The task reaches more than the endpoints and S3 on 443."
  }

  assert {
    condition = (
      output.deploy.task_subnets == tolist(["subnet-m1", "subnet-m2"]) &&
      output.deploy.tasks["eks-deployer"].container == "eks-deployer" &&
      output.family == "agentic-platform-eks-deployer"
    )
    error_message = "The deploy output does not name the management subnets and the container."
  }

  assert {
    condition     = aws_cloudwatch_log_group.task[0].kms_key_id == "arn:aws:kms:us-east-1:111111111111:key/logs"
    error_message = "The task log group is not encrypted under the module key."
  }
}
