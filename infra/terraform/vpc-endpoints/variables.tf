variable "create_endpoints" {
  description = "Master toggle. When false the module provisions nothing, so an account that already has endpoints can skip it."
  type        = bool
  default     = false
}

variable "region" {
  description = "AWS region hosting the endpoints."
  type        = string
  default     = "us-east-1"
}

variable "vpc_id" {
  description = "VPC in which to create the endpoints."
  type        = string
  default     = ""
}

variable "subnet_ids" {
  description = "Private subnets for the interface endpoint ENIs."
  type        = list(string)
  default     = []
}

variable "interface_services" {
  description = "Short service names to create as interface endpoints, for example bedrock-runtime, ssmmessages, ecr.api, ecr.dkr, logs, kms."
  type        = list(string)
  default     = []
}

variable "create_s3_gateway" {
  description = "Create the S3 gateway endpoint, which ECR image layers require."
  type        = bool
  default     = true
}

variable "route_table_ids" {
  description = "Route tables to associate with the S3 gateway endpoint. Empty auto-discovers the route tables for subnet_ids."
  type        = list(string)
  default     = []
}

variable "allowed_cidr_blocks" {
  description = "CIDR blocks permitted to reach the endpoints on 443. Use the in-VPC subnet CIDRs, never a public range."
  type        = list(string)
  default     = []
}

variable "private_dns_enabled" {
  description = "Enable private DNS so each regional service name resolves to the endpoint in-VPC."
  type        = bool
  default     = true
}

variable "name_prefix" {
  description = "Name prefix for created resources."
  type        = string
  default     = "agentic-platform"
}

variable "tags" {
  description = "Tags applied to all created resources."
  type        = map(string)
  default     = {}
}
