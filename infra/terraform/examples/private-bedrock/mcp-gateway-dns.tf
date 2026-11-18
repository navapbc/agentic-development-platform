locals {
  eks_mcp_gateway_exposed = local.eks_enabled && var.eks_mcp_gateway_certificate_arn != ""
  eks_mcp_gateway_hosts = local.eks_mcp_gateway_exposed ? {
    for name, tenant in local.tenants : name => coalesce(tenant.mcp_gateway_host, "mcp-${name}.${var.eks_mcp_gateway_domain}")
  } : {}
}

resource "aws_route53_zone" "mcp_gateway" {
  count = local.eks_mcp_gateway_exposed ? 1 : 0

  name    = var.eks_mcp_gateway_domain
  comment = "Host names of each tenant's MCP gateway load balancer."
  # inv eks-deploy writes the records, which Terraform does not track, so a destroy deletes them
  # with the zone.
  force_destroy = true

  vpc {
    vpc_id = local.vpc_id
  }

  tags = var.tags

  lifecycle {
    precondition {
      condition     = var.eks_mcp_gateway_domain != ""
      error_message = "eks_mcp_gateway_certificate_arn needs eks_mcp_gateway_domain, the domain each tenant's MCP gateway is named under."
    }
    precondition {
      condition = alltrue([
        for host in values(local.eks_mcp_gateway_hosts) :
        can(regex("^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", trimsuffix(host, ".${var.eks_mcp_gateway_domain}"))) && endswith(host, ".${var.eks_mcp_gateway_domain}")
      ])
      error_message = "A tenant's mcp_gateway_host is <label>.<eks_mcp_gateway_domain>, which the gateway zone and a certificate for *.<eks_mcp_gateway_domain> cover."
    }
    precondition {
      condition     = var.eks_mcp_gateway_certificate_arn != var.eks_webhook_certificate_arn
      error_message = "eks_mcp_gateway_certificate_arn must cover *.<eks_mcp_gateway_domain>, which the inbound webhook certificate does not. Issue and import one with inv mcp-gateway-certs."
    }
    precondition {
      condition     = length(distinct(values(local.eks_mcp_gateway_hosts))) == length(local.eks_mcp_gateway_hosts)
      error_message = "Each tenant's MCP gateway needs its own host name."
    }
  }
}
