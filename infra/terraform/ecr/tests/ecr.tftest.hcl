mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "111111111111" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
}

variables {
  repository_name = "agentic-platform/platform"
}

run "disabled_module_creates_nothing" {
  command = plan

  assert {
    condition     = length(aws_ecr_repository.this) == 0 && length(aws_ecr_lifecycle_policy.this) == 0 && length(aws_kms_key.this) == 0
    error_message = "The disabled module planned a resource."
  }
}

run "lifecycle_keeps_the_newest_images_of_each_architecture_and_their_signatures" {
  command = plan

  variables {
    create_repository = true
  }

  assert {
    condition = jsondecode(aws_ecr_lifecycle_policy.this[0].policy).rules == [
      {
        rulePriority = 1
        description  = "Keep the referrers indexes that protect the signatures of the retained images."
        selection    = { tagStatus = "tagged", tagPatternList = ["sha256-*"], countType = "imageCountMoreThan", countNumber = 100 }
        action       = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Keep the newest arm64 images."
        selection    = { tagStatus = "tagged", tagPatternList = ["*-arm64"], countType = "imageCountMoreThan", countNumber = 5 }
        action       = { type = "expire" }
      },
      {
        rulePriority = 3
        description  = "Keep the newest amd64 images."
        selection    = { tagStatus = "tagged", tagPatternList = ["*-amd64"], countType = "imageCountMoreThan", countNumber = 5 }
        action       = { type = "expire" }
      },
      {
        rulePriority = 4
        description  = "Keep the newest images with any other tag."
        selection    = { tagStatus = "tagged", tagPatternList = ["*"], countType = "imageCountMoreThan", countNumber = 5 }
        action       = { type = "expire" }
      },
      {
        rulePriority = 5
        description  = "Expire untagged images."
        selection    = { tagStatus = "untagged", countType = "sinceImagePushed", countUnit = "days", countNumber = 14 }
        action       = { type = "expire" }
      },
    ]
    error_message = "The lifecycle policy does not keep 100 referrers indexes, 5 images of each architecture, 5 images with any other tag, and 14 days of untagged images, in that priority order."
  }
}

run "retention_counts_are_configurable" {
  command = plan

  variables {
    create_repository              = true
    retained_image_count           = 3
    retained_signature_index_count = 40
    untagged_expiry_days           = 7
  }

  assert {
    condition     = [for rule in jsondecode(aws_ecr_lifecycle_policy.this[0].policy).rules : rule.selection.countNumber] == [40, 3, 3, 3, 7]
    error_message = "The lifecycle rules ignore retained_image_count, retained_signature_index_count, or untagged_expiry_days."
  }
}

run "retained_image_count_rejects_zero" {
  command = plan

  variables {
    create_repository    = true
    retained_image_count = 0
  }

  expect_failures = [var.retained_image_count]
}

run "signature_index_count_covers_both_architectures" {
  command = plan

  variables {
    create_repository              = true
    retained_image_count           = 5
    retained_signature_index_count = 9
  }

  expect_failures = [var.retained_signature_index_count]
}
