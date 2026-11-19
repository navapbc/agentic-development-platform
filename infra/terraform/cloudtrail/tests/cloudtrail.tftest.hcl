mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "111111111111" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  mock_resource "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:us-east-1:111111111111:key/00000000-0000-0000-0000-000000000000" }
  }
}

variables {
  create_cloudtrail = true
  access_log_bucket = "agentic-platform-access-logs"
}

run "without_data_events_the_trail_keeps_its_default_management_recording" {
  command = plan

  assert {
    condition     = length(aws_cloudtrail.this[0].advanced_event_selector) == 0
    error_message = "The trail replaced its default management-event recording with no data events to record."
  }
}

run "a_listed_function_records_its_invocations_beside_the_management_events" {
  command = plan

  variables {
    data_event_function_arns = ["arn:aws:lambda:us-east-1:111111111111:function:agentic-platform-off-hours"]
  }

  assert {
    condition = (
      [for selector in aws_cloudtrail.this[0].advanced_event_selector : selector.name] == ["Management events", "Invocations of the listed functions"] &&
      jsonencode(one([for selector in aws_cloudtrail.this[0].advanced_event_selector : [for field in selector.field_selector : field.equals if field.field == "resources.type"] if selector.name == "Invocations of the listed functions"])) == jsonencode([["AWS::Lambda::Function"]]) &&
      jsonencode(one([for selector in aws_cloudtrail.this[0].advanced_event_selector : [for field in selector.field_selector : field.equals if field.field == "resources.ARN"] if selector.name == "Invocations of the listed functions"])) == jsonencode([var.data_event_function_arns])
    )
    error_message = "The trail does not record the invocations of the listed function alongside the management events."
  }
}

run "buckets_and_functions_each_get_their_own_selector" {
  command = plan

  variables {
    data_event_bucket_arns   = ["arn:aws:s3:::agentic-platform-tickets"]
    data_event_function_arns = ["arn:aws:lambda:us-east-1:111111111111:function:agentic-platform-off-hours"]
  }

  assert {
    condition     = [for selector in aws_cloudtrail.this[0].advanced_event_selector : selector.name] == ["Management events", "Object-level events on the listed buckets", "Invocations of the listed functions"]
    error_message = "The trail does not record the bucket objects and the function invocations under separate selectors."
  }
}
