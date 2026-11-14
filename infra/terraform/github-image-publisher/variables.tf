variable "create_github_image_publisher" {
  description = "Create the role GitHub Actions assumes to push the platform images."
  type        = bool
  default     = false
}

variable "name_prefix" {
  description = "Prefix for resource names."
  type        = string
  default     = "agentic-platform"
}

variable "oidc_provider_arn" {
  description = "ARN of the account's GitHub OIDC provider for token.actions.githubusercontent.com. Empty creates one. An account holds one provider per URL, so pass the ARN when another stack already created it."
  type        = string
  default     = ""
}

variable "github_repository" {
  description = "The owner/name of the only repository whose workflows may assume the role."
  type        = string
  default     = "navapbc/agentic-development-platform"

  validation {
    condition     = can(regex("^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$", var.github_repository))
    error_message = "github_repository must be owner/name."
  }
}

variable "github_repository_ids" {
  description = "The numeric owner and repository IDs, for a repository whose OIDC tokens carry GitHub's immutable subject, repo:<owner>@<owner_id>/<name>@<repository_id>:ref:<ref>. Null matches the name form, repo:<owner>/<name>:ref:<ref>. Read the form a repository uses with gh api repos/<owner>/<name>/actions/oidc/customization/sub."
  type = object({
    owner_id      = number
    repository_id = number
  })
  default = null
}

variable "allowed_refs" {
  description = "The git refs whose workflow runs may assume the role, as full ref names. A trailing * matches a prefix, so refs/tags/v* admits every v tag."
  type        = list(string)
  default     = ["refs/heads/main", "refs/tags/v*"]

  validation {
    condition     = length(var.allowed_refs) > 0 && alltrue([for ref in var.allowed_refs : startswith(ref, "refs/heads/") || startswith(ref, "refs/tags/")])
    error_message = "allowed_refs must name at least one ref, each under refs/heads/ or refs/tags/."
  }
}

variable "repository_arns" {
  description = "ARNs of the ECR repositories the role may push to, the platform image repositories."
  type        = list(string)
  default     = []
}

variable "signing_profile_arn" {
  description = "ARN of the AWS Signer profile the role signs the platform images with. Empty grants no signing."
  type        = string
  default     = ""
}

variable "config_store" {
  description = "The tenant config bucket and its KMS key. The publish role then prunes platform/image_exceptions.yaml and reads every tenant's image_exceptions.yaml for the daily review, and the image-exception role is created to write platform/image_exceptions.yaml. Null grants neither."
  type = object({
    bucket_arn  = string
    kms_key_arn = string
  })
  default = null
}

variable "exception_environment" {
  description = "The GitHub environment the image-exception workflow runs in. The image-exception role trusts only jobs of this environment, whose required reviewers approve each exception."
  type        = string
  default     = "image-exception"
}

variable "pull_repository_arns" {
  description = "ECR repository ARNs the image-exception role may pull from to scan an image, besides repository_arns, such as the pull-through cache repositories."
  type        = list(string)
  default     = []
}

variable "tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = {}
}
