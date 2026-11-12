module "github_image_publisher" {
  source = "../../github-image-publisher"

  create_github_image_publisher = var.create_github_image_publisher
  name_prefix                   = var.name_prefix
  oidc_provider_arn             = var.github_oidc_provider_arn
  github_repository             = var.github_repository
  github_repository_ids         = var.github_repository_ids
  allowed_refs                  = var.github_publish_refs
  repository_arns               = values(module.container_services.repository_arns)
  signing_profile_arn           = try(aws_signer_signing_profile.platform[0].arn, "")
  tags                          = var.tags
}
