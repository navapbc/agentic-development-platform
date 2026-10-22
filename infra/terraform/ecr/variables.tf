variable "create_repository" {
  description = "Master toggle. When false the module provisions nothing, so an existing registry can be reused."
  type        = bool
  default     = false
}

variable "repository_name" {
  description = "ECR repository name."
  type        = string
}

variable "kms_key_arn" {
  description = "Customer-managed KMS key ARN for repository encryption. Empty creates a key owned by this module."
  type        = string
  default     = ""
}

variable "force_delete" {
  description = "Allow Terraform to delete the repository even when it holds images."
  type        = bool
  default     = true
}

variable "untagged_expiry_days" {
  description = "Days after which untagged images expire."
  type        = number
  default     = 14
}

variable "name_prefix" {
  description = "Name prefix for resource tag names."
  type        = string
  default     = "agentic-platform"
}

variable "tags" {
  description = "Tags applied to all created resources."
  type        = map(string)
  default     = {}
}
