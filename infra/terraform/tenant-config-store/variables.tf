variable "create_tenant_config_store" {
  description = "Create the tenant config bucket and its KMS key."
  type        = bool
  default     = false
}

variable "name_prefix" {
  description = "Prefix for the bucket and key alias names."
  type        = string
}

variable "tenants" {
  description = "Principal ARNs of each tenant's prefix, tenants/<tenant>/. publishers write and delete the tenant's bundle, and readers read it. Every other principal except admin_principals is denied the prefix. The ARNs appear only in policy conditions, so a role may be created after the bucket."
  type = map(object({
    publishers = list(string)
    readers    = list(string)
  }))
  default = {}
}

variable "admin_principals" {
  description = "IAM principal ARNs allowed to read, write, and delete every object, for restoring a version and emptying the bucket before a destroy."
  type        = list(string)
  default     = []
}

variable "noncurrent_version_retention_days" {
  description = "Days a replaced bundle file stays restorable, which bounds how far back a tenant's config history reaches in the bucket."
  type        = number
  default     = 365

  validation {
    condition     = var.noncurrent_version_retention_days >= 1
    error_message = "noncurrent_version_retention_days must be at least 1."
  }
}

variable "access_log_bucket" {
  description = "Target bucket for S3 server access logs, such as the bucket the access-logs module creates."
  type        = string

  validation {
    condition     = length(trim(var.access_log_bucket, " ")) > 0
    error_message = "access_log_bucket must name the bucket that receives S3 server access logs."
  }
}

variable "tags" {
  description = "Tags applied to the created resources."
  type        = map(string)
  default     = {}
}
