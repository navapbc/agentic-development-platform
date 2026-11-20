variable "create_bucket" {
  description = "Master toggle. When false the module provisions nothing, so an existing access-log bucket can be reused."
  type        = bool
  default     = false
}

variable "bucket_name" {
  description = "Name of the access-log bucket. Empty derives <name_prefix>-access-logs-<account id>."
  type        = string
  default     = ""
}

variable "name_prefix" {
  description = "Prefix for the derived bucket name."
  type        = string
}

variable "cold_storage_after_days" {
  description = "Days an access log stays in S3 Standard before it moves to Glacier Instant Retrieval. OMB M-21-31 sets 12 months of active storage."
  type        = number
  default     = 365
}

variable "retention_days" {
  description = "Days an access log is kept before it expires. OMB M-21-31 sets 12 months of active storage followed by 18 months of cold storage."
  type        = number
  default     = 913

  validation {
    condition     = var.retention_days > var.cold_storage_after_days
    error_message = "retention_days must be greater than cold_storage_after_days."
  }
}

variable "tags" {
  description = "Tags applied to the created resources."
  type        = map(string)
  default     = {}
}
