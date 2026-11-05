variable "create_container_services" {
  description = "Create the platform container services and their image repositories."
  type        = bool
  default     = false
}

variable "name_prefix" {
  description = "Prefix for the repository and resource names."
  type        = string
}

variable "kms_key_arn" {
  description = "Customer-managed KMS key ARN that encrypts the image repositories. Empty creates one key per repository."
  type        = string
  default     = ""
}

variable "tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = {}
}

variable "vpc_id" {
  description = "VPC the internal load balancer and its private hosted zone belong to."
  type        = string
  default     = ""
}

variable "application_subnet_ids" {
  description = "Application tier subnets in at least two Availability Zones, for the internal load balancer and the services."
  type        = list(string)
  default     = []
}

variable "internal_acm_certificate_arn" {
  description = "ACM certificate the internal load balancer presents, naming each internal service under internal_domain. Empty creates no internal load balancer."
  type        = string
  default     = ""

  validation {
    condition     = var.internal_acm_certificate_arn == "" || can(regex("^arn:aws[a-z-]*:acm:[a-z0-9-]+:[0-9]{12}:certificate/", var.internal_acm_certificate_arn))
    error_message = "internal_acm_certificate_arn must be an ACM certificate ARN."
  }
}

variable "internal_domain" {
  description = "Private hosted zone that names each internal service, such as coordination.<internal_domain>."
  type        = string
  default     = "platform.internal"
}

variable "internal_allowed_cidr_blocks" {
  description = "CIDRs allowed to reach the internal load balancer in addition to the members of its client security group."
  type        = list(string)
  default     = []
}

variable "access_log_bucket" {
  description = "Bucket that receives the S3 server access logs of the load balancer log bucket, such as the bucket the access-logs module creates."
  type        = string
  default     = ""
}

variable "load_balancer_log_retention_days" {
  description = "Days the internal load balancer access logs are kept."
  type        = number
  default     = 365
}
