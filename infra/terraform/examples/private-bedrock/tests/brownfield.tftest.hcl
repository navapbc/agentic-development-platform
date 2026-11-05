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
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
}

override_module {
  target = module.ecs_cluster
  outputs = {
    cluster_arn          = "arn:aws:ecs:us-east-1:111111111111:cluster/agentic-platform-ecs"
    cluster_name         = "agentic-platform-ecs"
    deployer_policy_arn  = "arn:aws:iam::111111111111:policy/agentic-platform-ecs-deployer"
    exec_task_policy_arn = "arn:aws:iam::111111111111:policy/agentic-platform-ecs-exec"
  }
}

override_module {
  target = module.aurora_postgresql
  outputs = {
    endpoint               = "agentic-platform.cluster-abc.us-east-1.rds.amazonaws.com"
    cluster_arn            = "arn:aws:rds:us-east-1:111111111111:cluster:agentic-platform"
    master_user_secret_arn = "arn:aws:secretsmanager:us-east-1:111111111111:secret:rds-master-x"
  }
}

override_module {
  target = module.secret_store
  outputs = {
    kms_key_arn                  = "arn:aws:kms:us-east-1:111111111111:key/secrets"
    secret_arns                  = {}
    database_secret_arns         = { temporal = "arn:aws:secretsmanager:us-east-1:111111111111:secret:agentic-platform/database/temporal-x" }
    role_bootstrap_function_name = "agentic-platform-secret-role-bootstrap"
  }
}

variables {
  name_prefix       = "agentic-platform"
  access_log_bucket = "agentic-platform-access-logs-111111111111"

  create_temporal_control_plane = true
  temporal_server_image         = "111111111111.dkr.ecr.us-east-1.amazonaws.com/agentic-platform/temporal-server:1.32.1-a-arm64@sha256:1"
  temporal_admin_tools_image    = "111111111111.dkr.ecr.us-east-1.amazonaws.com/agentic-platform/temporal-admin-tools:1.32.1-b-arm64@sha256:2"
}

run "a_created_network_carries_the_control_plane" {
  command = plan

  variables {
    create_network     = true
    availability_zones = ["us-east-1a", "us-east-1b"]
  }

  assert {
    condition     = length(local.data_cidrs) > 0
    error_message = "A created network supplies no data-tier CIDRs."
  }
}

run "an_existing_network_carries_the_control_plane" {
  command = plan

  variables {
    create_network         = false
    vpc_id                 = "vpc-1"
    subnet_ids             = ["subnet-app-a", "subnet-app-b"]
    endpoint_allowed_cidrs = ["10.0.1.0/24", "10.0.2.0/24"]
    management_subnet_ids  = ["subnet-mgmt-a", "subnet-mgmt-b"]
    management_cidr_blocks = ["10.0.21.0/24", "10.0.22.0/24"]
    data_subnet_ids        = ["subnet-data-a", "subnet-data-b"]
  }

  assert {
    condition     = length(local.data_cidrs) > 0
    error_message = "An existing network supplies no data-tier CIDRs, so the server security group cannot reach Aurora."
  }
}
