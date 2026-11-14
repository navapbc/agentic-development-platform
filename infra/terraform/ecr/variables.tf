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

variable "retained_image_count" {
  description = "Number of most recently pushed images the repository keeps with a -arm64 tag, with a -amd64 tag, and with any other tag. Older tagged images expire, and ECR removes their signatures."
  type        = number
  default     = 5

  validation {
    condition     = var.retained_image_count >= 1 && floor(var.retained_image_count) == var.retained_image_count
    error_message = "retained_image_count must be a whole number of at least 1."
  }
}

variable "retained_signature_index_count" {
  description = "Number of most recently pushed notation referrers indexes, tagged sha256-<digest>, the repository keeps. Each index protects the signatures of one image from the untagged expiry, and the indexes of expired images keep their places until newer indexes displace them."
  type        = number
  default     = 100

  validation {
    condition     = floor(var.retained_signature_index_count) == var.retained_signature_index_count && var.retained_signature_index_count >= 2 * var.retained_image_count
    error_message = "retained_signature_index_count must be a whole number of at least twice retained_image_count."
  }
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
