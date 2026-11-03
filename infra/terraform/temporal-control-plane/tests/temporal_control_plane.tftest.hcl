mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "111111111111" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
}

variables {
  name_prefix = "agentic-platform"
}

run "disabled_module_creates_nothing" {
  command = plan

  assert {
    condition     = output.server_repository_name == null && output.admin_tools_repository_name == null
    error_message = "The disabled module planned an image repository."
  }
}

run "repositories_hold_the_server_and_admin_tools_images" {
  command = plan

  variables {
    create_temporal_control_plane = true
  }

  assert {
    condition     = output.server_repository_name == "agentic-platform/temporal-server"
    error_message = "The server repository is not <name_prefix>/temporal-server."
  }

  assert {
    condition     = output.admin_tools_repository_name == "agentic-platform/temporal-admin-tools"
    error_message = "The admin-tools repository is not <name_prefix>/temporal-admin-tools."
  }
}
