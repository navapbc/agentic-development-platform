variable "region" {
  description = "AWS region for the state bucket. Use the region of the composition roots it serves."
  type        = string
  default     = "us-east-1"
}

variable "name_prefix" {
  description = "Name prefix applied to the state bucket."
  type        = string
  default     = "agentic-platform"
}

variable "kms_key_arn" {
  description = "KMS key ARN for state encryption. Empty uses SSE-S3 AES256."
  type        = string
  default     = ""
}

variable "create_access_log_bucket" {
  description = "Create the bucket that receives S3 server access logs from every platform bucket. Set false to use an existing bucket named by access_log_bucket."
  type        = bool
  default     = true
}

variable "access_log_bucket" {
  description = "Name of the access-log bucket. When create_access_log_bucket is true it names the created bucket, and empty derives <name_prefix>-access-logs-<account id>. When false it must name an existing bucket in this region."
  type        = string
  default     = ""

  validation {
    condition     = var.create_access_log_bucket || length(trim(var.access_log_bucket, " ")) > 0
    error_message = "access_log_bucket must name an existing bucket when create_access_log_bucket is false."
  }
}

variable "tags" {
  description = "Tags applied to the created resources."
  type        = map(string)
  default     = {}
}
