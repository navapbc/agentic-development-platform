variable "create_evidence_store" {
  description = "Master toggle. When false the module provisions nothing, so an existing store can be reused."
  type        = bool
  default     = false
}

variable "name_prefix" {
  description = "Prefix for the evidence bucket name."
  type        = string
}

variable "object_lock_mode" {
  description = "Default S3 Object Lock retention mode. COMPLIANCE forbids any early delete or overwrite."
  type        = string
  default     = "COMPLIANCE"

  validation {
    condition     = contains(["COMPLIANCE", "GOVERNANCE"], var.object_lock_mode)
    error_message = "object_lock_mode must be COMPLIANCE or GOVERNANCE."
  }
}

variable "retention_days" {
  description = "Default Object Lock retention window in days applied to every evidence object."
  type        = number
  default     = 2555
}

variable "kms_key_arn" {
  description = "KMS key ARN for bucket encryption. Empty uses SSE-S3 AES256."
  type        = string
  default     = ""
}

variable "writer_principal_list" {
  description = "IAM principal ARNs allowed to write and read evidence objects, beyond the account root."
  type        = list(string)
  default     = []
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
