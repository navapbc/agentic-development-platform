variable "region" {
  description = "AWS region for all resources."
  type        = string
  default     = "us-east-1"
}

variable "name_prefix" {
  description = "Name prefix applied to created resources."
  type        = string
  default     = "agentic-platform"
}

variable "tags" {
  description = "Tags applied to all created resources."
  type        = map(string)
  default     = {}
}

variable "create_network" {
  description = "Create a private VPC and subnets. Set false to use an existing network and supply vpc_id, subnet_ids, and endpoint_allowed_cidrs."
  type        = bool
  default     = false
}

variable "vpc_cidr" {
  description = "CIDR for the VPC when create_network is true."
  type        = string
  default     = "10.0.0.0/16"
}

variable "new_subnet_cidrs" {
  description = "CIDRs for the private subnets to create when create_network is true. One subnet per CIDR."
  type        = list(string)
  default     = ["10.0.1.0/24"]
}

variable "availability_zones" {
  description = "AZs for the created subnets, aligned to new_subnet_cidrs. Empty selects the first available AZs in the region."
  type        = list(string)
  default     = []
}

variable "vpc_id" {
  description = "Existing VPC id. Required when create_network is false."
  type        = string
  default     = ""
}

variable "subnet_ids" {
  description = "Existing private subnet ids. Required when create_network is false."
  type        = list(string)
  default     = []
}

variable "endpoint_allowed_cidrs" {
  description = "CIDRs permitted to reach the Bedrock endpoint on 443. Required when create_network is false. Ignored when create_network is true, where the created subnet CIDRs are used."
  type        = list(string)
  default     = []
}

variable "route_table_ids" {
  description = "Route tables for the jumphost S3 gateway endpoint when create_network is false. Empty auto-discovers the route tables for subnet_ids."
  type        = list(string)
  default     = []
}

variable "create_cloudtrail" {
  description = "Create a single-region management CloudTrail trail and its log bucket. Set false when an organization trail already covers this account."
  type        = bool
  default     = false
}

variable "create_endpoints" {
  description = "Create the shared VPC endpoints. Set false when the account already has bedrock-runtime, ssmmessages, ECR, logs, and kms endpoints."
  type        = bool
  default     = true
}

variable "create_ecr_repository" {
  description = "Create the jumphost image repository. Set false to reuse an existing registry, and set existing_ecr_repository_url and existing_ecr_repository_arn."
  type        = bool
  default     = true
}

variable "jumphost_repository_name" {
  description = "ECR repository name for the jumphost-base image when create_ecr_repository is true."
  type        = string
  default     = "agentic-platform/jumphost-base"
}

variable "existing_endpoints_security_group_id" {
  description = "Endpoint security group id to use when create_endpoints is false. The jumphost task is allowed 443 egress to it."
  type        = string
  default     = ""
}

variable "existing_ecr_repository_url" {
  description = "ECR repository URL to use when create_ecr_repository is false, without a tag."
  type        = string
  default     = ""
}

variable "existing_ecr_repository_arn" {
  description = "ECR repository ARN to use when create_ecr_repository is false."
  type        = string
  default     = ""
}

variable "cloudtrail_name" {
  description = "Name of the CloudTrail trail when create_cloudtrail is true."
  type        = string
  default     = "agentic-platform-management"
}

variable "jumphost_name" {
  description = "Logical jumphost name, matching JUMPHOST_NAME in the platform .env."
  type        = string
  default     = "bedrock-jumphost"
}

variable "jumphost_image_tag" {
  description = "Tag of the jumphost-base image the task runs. Push this tag with inv publish-jumphost-image before connecting."
  type        = string
  default     = "v1"
}

variable "jumphost_session_duration_seconds" {
  description = "Default jumphost task TTL. The tunnel can override it per session."
  type        = number
  default     = 3600
}
