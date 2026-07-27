variable "create_bedrock_vpce" {
  description = "Master toggle. When false the module provisions nothing, so it can be wired into a root module and left off by default."
  type        = bool
  default     = false
}

variable "region" {
  description = "AWS region hosting the bedrock-runtime interface endpoint."
  type        = string
  default     = "us-east-1"
}

variable "vpc_id" {
  description = "VPC in which to create the interface endpoint."
  type        = string
  default     = ""
}

variable "subnet_ids" {
  description = "Private subnets for the interface endpoint ENIs."
  type        = list(string)
  default     = []
}

variable "allowed_cidr_blocks" {
  description = "CIDR blocks permitted to reach the endpoint on 443 (VPC app/data subnets, never a public range)."
  type        = list(string)
  default     = []
}

variable "private_dns_enabled" {
  description = "Enable private DNS so bedrock-runtime resolves to the endpoint's private IPs in-VPC. Keeps the path private by default."
  type        = bool
  default     = true
}

variable "create_invoke_policy" {
  description = "Also create an IAM policy granting bedrock:InvokeModel for principals to attach."
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
