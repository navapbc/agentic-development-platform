variable "create_invoke_policy" {
  description = "Master toggle. When false the module provisions nothing."
  type        = bool
  default     = false
}

variable "model_resources" {
  description = "ARNs the invoke policy grants against. Defaults to foundation models and inference profiles in any region."
  type        = list(string)
  default = [
    "arn:aws:bedrock:*::foundation-model/*",
    "arn:aws:bedrock:*:*:inference-profile/*",
    "arn:aws:bedrock:*:*:application-inference-profile/*",
  ]
}

variable "include_discovery" {
  description = "Also grant read-only model and inference-profile discovery actions."
  type        = bool
  default     = true
}

variable "name_prefix" {
  description = "Name prefix for the policy."
  type        = string
  default     = "agentic-platform"
}

variable "tags" {
  description = "Tags applied to the policy."
  type        = map(string)
  default     = {}
}
