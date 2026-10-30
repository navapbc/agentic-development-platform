variable "create_cloudtrail" {
  description = "Master toggle. When false the module provisions nothing."
  type        = bool
  default     = false
}

variable "trail_name" {
  description = "Name of the CloudTrail trail."
  type        = string
  default     = "agentic-platform-management"
}

variable "region" {
  description = "AWS region the trail is created in, used to build the trail ARN in the bucket policy."
  type        = string
  default     = "us-east-1"
}

variable "is_multi_region_trail" {
  description = "Capture control-plane activity in every region. Defaults to true for FedRAMP audit coverage."
  type        = bool
  default     = true
}

variable "data_event_bucket_arns" {
  description = "ARNs of buckets whose object-level reads and writes the trail records as data events, including denied requests. Empty records management events only."
  type        = list(string)
  default     = []
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

variable "access_log_bucket" {
  description = "Target bucket for S3 server access logs on the trail bucket, such as the bucket the access-logs module creates."
  type        = string

  validation {
    condition     = length(trim(var.access_log_bucket, " ")) > 0
    error_message = "access_log_bucket must name the bucket that receives S3 server access logs."
  }
}
