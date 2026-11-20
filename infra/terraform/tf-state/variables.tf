variable "create_state_bucket" {
  description = "Master toggle. When false the module provisions nothing, so an existing state bucket can be reused."
  type        = bool
  default     = false
}

variable "name_prefix" {
  description = "Prefix for the state bucket name."
  type        = string
}

variable "kms_key_arn" {
  description = "KMS key ARN for bucket encryption. Empty uses SSE-S3 AES256."
  type        = string
  default     = ""
}

variable "noncurrent_version_retention_days" {
  description = "Days a superseded state version is kept before it expires. Versions allow a state file to be restored after a bad apply."
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

variable "tags" {
  description = "Tags applied to the created resources."
  type        = map(string)
  default     = {}
}
