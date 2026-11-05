variable "create_container_services" {
  description = "Create the platform container services and their image repositories."
  type        = bool
  default     = false
}

variable "name_prefix" {
  description = "Prefix for the repository and resource names."
  type        = string
}

variable "kms_key_arn" {
  description = "Customer-managed KMS key ARN that encrypts the image repositories. Empty creates one key per repository."
  type        = string
  default     = ""
}

variable "tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = {}
}
