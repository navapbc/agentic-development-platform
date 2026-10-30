variable "create_rebar_ticket_store" {
  description = "Create the rebar ticket store bucket, its KMS key, and, unless existing role ARNs are set, its reader and writer roles."
  type        = bool
  default     = false
}

variable "name_prefix" {
  description = "Prefix for the bucket, key alias, and role names."
  type        = string
}

variable "prefix" {
  description = "Key prefix the ticket store syncs under, the path part of its s3:// remote URL."
  type        = string
  default     = "tickets"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9._-]*$", var.prefix))
    error_message = "prefix must be one lowercase path segment without slashes."
  }
}

variable "trusted_principals" {
  description = "IAM principal ARNs allowed to assume the created reader and writer roles, such as the platform task roles or an operator. Required when the module creates the roles."
  type        = list(string)
  default     = []
}

variable "existing_reader_role_arn" {
  description = "ARN of an existing role that fetches the store. Set it together with existing_writer_role_arn to supply both roles, and the module then creates none."
  type        = string
  default     = ""
}

variable "existing_writer_role_arn" {
  description = "ARN of an existing role that pushes the store. Set it together with existing_reader_role_arn."
  type        = string
  default     = ""
}

variable "admin_principals" {
  description = "IAM principal ARNs allowed to read, write, and delete objects, for seeding the store and emptying the bucket before a destroy. Every other principal except the reader and writer is denied object access."
  type        = list(string)
  default     = []
}

variable "noncurrent_version_retention_days" {
  description = "Days a replaced bundle version stays restorable. Every push replaces the previous bundle, so this bounds how far back a bad push can be undone."
  type        = number
  default     = 90

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

variable "iam_path" {
  description = "Path for the created roles."
  type        = string
  default     = "/"
}

variable "permissions_boundary_arn" {
  description = "Permissions boundary for the created roles. Empty attaches none."
  type        = string
  default     = ""
}

variable "tags" {
  description = "Tags applied to the created resources."
  type        = map(string)
  default     = {}
}
