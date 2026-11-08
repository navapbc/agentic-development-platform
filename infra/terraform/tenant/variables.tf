variable "create_tenant" {
  description = "Create the tenant's key, secrets, worker policy, image prefix, config publisher, and ticket store."
  type        = bool
  default     = false
}

variable "name_prefix" {
  description = "Prefix of the deployment's resource names."
  type        = string
}

variable "tenant" {
  description = "Tenant name, which names its secrets, image prefix, config prefix, ticket store, and namespaces."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9]{1,15}$", var.tenant)) && var.tenant != "default"
    error_message = "tenant is 2 to 16 lowercase letters and digits starting with a letter, and may not be default."
  }
}

variable "description" {
  description = "What the tenant is, such as the team or project it serves."
  type        = string
}

variable "worker_role_arn" {
  description = "Pod Identity role of the tenant's activity worker, the only principal that reads the tenant's secrets."
  type        = string
  default     = ""
}

variable "secrets" {
  description = "Names of the tenant's secrets, each created empty under <name_prefix>/tenants/<tenant>/ for the tenant to fill, such as github_token."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for name in var.secrets : can(regex("^[a-z][a-z0-9_]*$", name))])
    error_message = "A secret name is lowercase letters, digits, and underscores, starting with a letter."
  }
}

variable "secret_recovery_window_days" {
  description = "Days a deleted tenant secret stays recoverable. 0 deletes it at once."
  type        = number
  default     = 30
}

variable "config_store" {
  description = "Bucket and key of the tenant config store, whose tenants/<tenant>/ prefix holds the tenant's config bundle."
  type = object({
    bucket_arn  = string
    kms_key_arn = string
  })
  default = null
}

variable "config_publisher" {
  description = "GitHub repository whose workflows publish the tenant's config bundle and push its images, through the account's GitHub OIDC provider. allowed_refs lists the full refs that may publish, where a trailing * matches a prefix. github_repository_ids selects GitHub's immutable subject form. Null creates no publisher role."
  type = object({
    oidc_provider_arn = string
    github_repository = string
    allowed_refs      = optional(list(string), ["refs/heads/main"])
    github_repository_ids = optional(object({
      owner_id      = number
      repository_id = number
    }))
  })
  default = null

  validation {
    condition     = var.config_publisher == null || can(regex("^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$", var.config_publisher.github_repository))
    error_message = "config_publisher.github_repository must be owner/name."
  }

  validation {
    condition     = var.config_publisher == null || try(length(var.config_publisher.allowed_refs) > 0 && alltrue([for ref in var.config_publisher.allowed_refs : startswith(ref, "refs/heads/") || startswith(ref, "refs/tags/")]), false)
    error_message = "config_publisher.allowed_refs must name at least one ref, each under refs/heads/ or refs/tags/."
  }
}

variable "untagged_image_expiry_days" {
  description = "Days an untagged image in a tenant repository is kept."
  type        = number
  default     = 14
}

variable "ticket_store" {
  description = "The tenant's rebar ticket store. operators are the principals besides the worker that may assume its reader and writer roles, admins seed and empty it, and writer_github_oidc_trust lets a workflow push it, as the rebar-ticket-store module describes."
  type = object({
    operators = optional(list(string), [])
    admins    = optional(list(string), [])
    writer_github_oidc_trust = optional(object({
      provider_arn      = string
      subjects          = list(string)
      job_workflow_refs = optional(list(string), [])
      audience          = optional(string, "sts.amazonaws.com")
    }))
  })
  default = {}
}

variable "access_log_bucket" {
  description = "Target bucket for the S3 server access logs of the tenant's ticket store."
  type        = string
}

variable "tags" {
  description = "Tags applied to every resource, beside the tenant tag the module adds."
  type        = map(string)
  default     = {}
}
