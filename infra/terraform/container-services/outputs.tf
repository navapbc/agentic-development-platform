output "repository_urls" {
  description = "ECR repository of each image, keyed by the name inv publish-platform-images uses. Empty when disabled."
  value       = var.create_container_services ? { for name, repository in module.repository : name => repository.repository_url } : {}
}
