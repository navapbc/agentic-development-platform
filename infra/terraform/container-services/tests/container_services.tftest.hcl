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

variables {
  name_prefix = "agentic-platform"
}

run "disabled_module_creates_nothing" {
  command = plan

  assert {
    condition     = length(output.repository_urls) == 0 && alltrue([for repository in module.repository : repository.repository_name == null])
    error_message = "The disabled module planned a repository."
  }
}

run "repositories_hold_every_image_the_services_run" {
  command = apply

  variables {
    create_container_services = true
  }

  assert {
    condition = toset([for repository in module.repository : repository.repository_name]) == toset([
      "agentic-platform/platform",
      "agentic-platform/repo-intelligence",
      "agentic-platform/otel-collector",
      "agentic-platform/langfuse-web",
      "agentic-platform/langfuse-worker",
      "agentic-platform/clickhouse",
      "agentic-platform/tls-sidecar",
    ])
    error_message = "A repository is missing or misnamed."
  }

  assert {
    condition     = keys(output.repository_urls) == sort(["platform", "repo-intelligence", "otel-collector", "langfuse-web", "langfuse-worker", "clickhouse", "tls-sidecar"])
    error_message = "The repository_urls output does not name every image."
  }
}
