variable "create_bucket" {
  description = "Master toggle. When false the module provisions nothing, so an existing bucket can be reused."
  type        = bool
  default     = false
}

variable "name_prefix" {
  description = "Prefix for the bucket name."
  type        = string
}

variable "kms_key_arn" {
  description = "KMS key ARN for bucket encryption. Empty uses SSE-S3 AES256."
  type        = string
  default     = ""
}

variable "noncurrent_version_retention_days" {
  description = "Days a replaced or deleted object version is kept before it expires."
  type        = number
  default     = 30

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
