output "server_repository_url" {
  description = "ECR repository for the server image that docker/temporal.Dockerfile builds, or null when disabled."
  value       = module.server_repository.repository_url
}

output "server_repository_name" {
  description = "Name of the server image repository, or null when disabled."
  value       = module.server_repository.repository_name
}

output "admin_tools_repository_url" {
  description = "ECR repository for the admin-tools image that docker/temporal-admin-tools.Dockerfile builds, or null when disabled."
  value       = module.admin_tools_repository.repository_url
}

output "admin_tools_repository_name" {
  description = "Name of the admin-tools image repository, or null when disabled."
  value       = module.admin_tools_repository.repository_name
}
