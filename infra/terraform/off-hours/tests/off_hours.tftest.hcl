mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "111111111111" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::111111111111:role/agentic-platform-off-hours" }
  }
  mock_resource "aws_ssm_parameter" {
    defaults = { arn = "arn:aws:ssm:us-east-1:111111111111:parameter/agentic-platform/off-hours/wake-counts" }
  }
}

variables {
  create_off_hours_schedule = true
  name_prefix               = "agentic-platform"
  region                    = "us-east-1"
  cluster_arns              = ["arn:aws:ecs:us-east-1:111111111111:cluster/agentic-platform-ecs"]
  foundation_services       = ["arn:aws:ecs:us-east-1:111111111111:service/agentic-platform-ecs/agentic-platform-temporal-server"]
  services = [
    "arn:aws:ecs:us-east-1:111111111111:service/agentic-platform-ecs/agentic-platform-coordination",
    "arn:aws:ecs:us-east-1:111111111111:service/agentic-platform-ecs/agentic-platform-workflow-worker-blue",
  ]
}

run "disabled_creates_nothing" {
  command = plan

  variables {
    create_off_hours_schedule = false
  }

  assert {
    condition     = length(aws_scheduler_schedule.scale) == 0 && length(aws_ssm_document.scale) == 0 && output.documents == {}
    error_message = "A disabled schedule created a resource."
  }
}

run "the_schedules_run_in_the_time_zone_with_weekends_off" {
  command = apply

  assert {
    condition = (
      aws_scheduler_schedule.scale["sleep"].schedule_expression == "cron(0 20 ? * * *)" &&
      aws_scheduler_schedule.scale["wake"].schedule_expression == "cron(0 7 ? * MON-FRI *)" &&
      alltrue([for schedule in aws_scheduler_schedule.scale : schedule.schedule_expression_timezone == "America/Chicago"]) &&
      jsondecode(aws_scheduler_schedule.scale["wake"].target[0].input).DocumentName == "agentic-platform-off-hours-wake"
    )
    error_message = "The schedules do not sleep nightly and wake on weekdays in America/Chicago."
  }
}

run "the_foundation_stops_last_and_starts_first" {
  command = apply

  assert {
    condition = (
      jsonencode([for step in yamldecode(aws_ssm_document.scale["sleep"].content).mainSteps : step.inputs.InputPayload.Services]) == jsonencode([var.services, var.foundation_services]) &&
      jsonencode([for step in yamldecode(aws_ssm_document.scale["wake"].content).mainSteps : step.inputs.InputPayload.Services]) == jsonencode([var.foundation_services, var.services]) &&
      yamldecode(aws_ssm_document.scale["wake"].content).mainSteps[0].inputs.InputPayload.WaitSeconds == 540
    )
    error_message = "The foundation services do not stop after the others and start, and settle, before them."
  }
}

run "a_weekend_wake_with_a_sunday_rotation_is_refused" {
  command = plan

  variables {
    wake_schedule = "cron(0 7 ? * * *)"
  }

  expect_failures = [terraform_data.rotation_guard]
}

run "a_weekend_wake_is_allowed_when_the_rotation_is_not_on_a_sunday" {
  command = plan

  variables {
    wake_schedule                = "cron(0 7 ? * * *)"
    rotation_schedule_expression = "cron(0 6 ? 1/3 WED#1 *)"
  }

  assert {
    condition     = length(terraform_data.rotation_guard) == 1
    error_message = "A weekend wake was refused although the rotation does not run on a Sunday."
  }
}
