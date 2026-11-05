data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

locals {
  account_id  = data.aws_caller_identity.current.account_id
  partition   = data.aws_partition.current.partition
  internal_lb = var.create_container_services && var.internal_acm_certificate_arn != ""

  # The load balancer re-encrypts to each task, which serves TLS on `port` with a certificate
  # the task issues itself. The collector speaks OTLP over gRPC, whose health check reports
  # UNIMPLEMENTED (12) from a live server.
  internal_services = {
    coordination = {
      short            = "coord"
      port             = 8130
      protocol_version = "HTTP1"
      health_path      = "/health"
      matcher          = "200"
    }
    repo-intelligence = {
      short            = "repo-intel"
      port             = 8120
      protocol_version = "HTTP1"
      health_path      = "/health"
      matcher          = "200"
    }
    otel-collector = {
      short            = "otel"
      port             = 4317
      protocol_version = "GRPC"
      health_path      = "/AWS.ALB/healthcheck"
      matcher          = "12"
    }
    langfuse = {
      short            = "langfuse"
      port             = 3443
      protocol_version = "HTTP1"
      health_path      = "/api/public/health"
      matcher          = "200"
    }
  }
  internal_lb_services = local.internal_lb ? local.internal_services : {}
}
