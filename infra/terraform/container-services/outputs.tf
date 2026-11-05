output "repository_urls" {
  description = "ECR repository of each image, keyed by the name inv publish-platform-images uses. Empty when disabled."
  value       = var.create_container_services ? { for name, repository in module.repository : name => repository.repository_url } : {}
}

output "internal_endpoints" {
  description = "The internal load balancer's client security group, the target security group and target group of each service, and each service's HTTPS URL. Null without the internal load balancer."
  value = local.internal_lb ? {
    client_security_group_id  = aws_security_group.internal_client[0].id
    target_security_group_ids = { for name, group in aws_security_group.internal_target : name => group.id }
    target_group_arns         = { for name, group in aws_lb_target_group.internal : name => group.arn }
    urls                      = { for name in keys(local.internal_lb_services) : name => "https://${name}.${var.internal_domain}" }
    domain                    = var.internal_domain
  } : null
}
