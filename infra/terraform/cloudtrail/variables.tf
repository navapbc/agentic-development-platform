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
