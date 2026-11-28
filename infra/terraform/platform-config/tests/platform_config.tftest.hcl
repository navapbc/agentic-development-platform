run "platform_files_pass_through" {
  command = plan

  variables {
    directory = "tests/fixtures/platform"
  }

  assert {
    condition     = length(output.files) == 1 && contains(keys(output.files), "integrations.yaml")
    error_message = "The platform config files do not pass through by name."
  }
}

run "no_directory_sends_no_file" {
  command = plan

  assert {
    condition     = output.files == {}
    error_message = "An empty directory sends a file."
  }
}

run "a_tenant_only_file_is_refused" {
  command = plan

  variables {
    directory = "tests/fixtures/tenant-file"
  }

  expect_failures = [var.directory]
}

run "image_exceptions_is_refused" {
  command = plan

  variables {
    directory = "tests/fixtures/exceptions"
  }

  expect_failures = [var.directory]
}
