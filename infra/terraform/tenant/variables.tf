variable "create_tenant" {
  description = "Create the tenant's key, secrets, worker and review worker policies, image prefix, config publisher, and ticket store."
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
  description = "Pod Identity role of the tenant's activity worker, the only principal that reads the tenant's secrets apart from repo_intelligence_reader_arns on the repository intelligence token, webhook_reader_arn on the escalation action key, and review_role_arn on the registry token and the review signing key."
  type        = string
  default     = ""
}

variable "worker_role_name" {
  description = "Name of the role worker_role_arn names. The ticket store roles trust the worker through an ARN built from this name, so the store depends on no resource outside the tenant and a teardown can keep it while it removes the worker role."
  type        = string
  default     = ""
}

variable "review_role_arn" {
  description = "Pod Identity role of the tenant's review worker, the only principal that reads the tenant's review signing key, and which reads the tenant's registry_token beside the worker."
  type        = string
  default     = ""
}

variable "evidence_store" {
  description = "Evidence store bucket the tenant's review worker writes, with its KMS key when the bucket uses SSE-KMS. Null grants no evidence write and keeps evidence in Postgres only."
  type = object({
    bucket_arn  = string
    kms_key_arn = optional(string, "")
  })
  default = null
}

variable "secrets" {
  description = "Names of the tenant's secrets, each created empty under <name_prefix>/tenants/<tenant>/ for the tenant to fill, such as github_token."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for name in var.secrets : can(regex("^[a-z][a-z0-9_]*$", name))])
    error_message = "A secret name is lowercase letters, digits, and underscores, starting with a letter."
  }

  validation {
    condition     = !contains(var.secrets, "repo_intelligence_token")
    error_message = "repo_intelligence_token is the generated secret the module creates for every tenant, so secrets may not name it."
  }

  validation {
    condition     = !contains(var.secrets, "mcp_gateway_token")
    error_message = "mcp_gateway_token is the generated secret the module creates for every tenant, so secrets may not name it."
  }

  validation {
    condition     = !contains(var.secrets, "escalation_action_key")
    error_message = "escalation_action_key is the generated secret the module creates for every tenant, so secrets may not name it."
  }

  validation {
    condition     = !contains(var.secrets, "review_signing_key")
    error_message = "review_signing_key is the secret the module creates for every tenant's review worker, so secrets may not name it."
  }
}

variable "repo_intelligence_reader_arns" {
  description = "Principals besides the tenant worker that read the tenant's repository intelligence token, the task role of the repository intelligence service that reads every tenant's current and previous token at runtime."
  type        = list(string)
  default     = []
}

variable "mcp_gateway_token_reader_arns" {
  description = "Principals besides the tenant worker that read the tenant's MCP gateway token, such as the operators who hand it to the tenant's MCP clients."
  type        = list(string)
  default     = []
}

variable "repo_intelligence_token_rotation" {
  description = "The rotation function of the repository intelligence token and its execution role, such as the token_rotation output of the secret-store module. The token's secret policy admits the role, whose identity policy grants the tenant key through Secrets Manager. Null leaves the token unrotated."
  type = object({
    function_arn = string
    role_arn     = string
  })
  default = null
}

variable "repo_intelligence_token_rotation_days" {
  description = "Days between rotations of the repository intelligence token when repo_intelligence_token_rotation is set."
  type        = number
  default     = 30

  validation {
    condition     = var.repo_intelligence_token_rotation_days >= 1 && var.repo_intelligence_token_rotation_days <= 1000 && floor(var.repo_intelligence_token_rotation_days) == var.repo_intelligence_token_rotation_days
    error_message = "repo_intelligence_token_rotation_days is a whole number from 1 to 1000."
  }
}

variable "escalation_action_key_version" {
  description = "Write-only version of the generated escalation action key and its SHA-256 fingerprint. Raising it writes a new key and its fingerprint in one apply. A tenant created before the fingerprint existed raises it once, because only then does the fingerprint match the key."
  type        = number
  default     = 1

  validation {
    condition     = var.escalation_action_key_version >= 1 && floor(var.escalation_action_key_version) == var.escalation_action_key_version
    error_message = "escalation_action_key_version is a whole number of at least 1."
  }
}

variable "slack_poster_reader_arn" {
  description = "Pod Identity role of the Slack posting service, which reads the fingerprint of the tenant's escalation action key, or empty without the shared Slack app."
  type        = string
  default     = ""
}

variable "secret_recovery_window_days" {
  description = "Days a deleted tenant secret stays recoverable. 0 deletes it at once."
  type        = number
  default     = 30
}

variable "config_store" {
  description = "Bucket and key of the tenant config store, whose tenants/<tenant>/ prefix holds the tenant's config bundle, and the principal tag key whose value, the tenant name, lets the publisher role write that prefix past the bucket policy, the publisher key of the tenant-config-store module's tenant_principal_tag_keys output."
  type = object({
    bucket_arn        = string
    kms_key_arn       = string
    publisher_tag_key = optional(string, "tenant-config-publisher")
  })
  default = null
}

variable "config_publisher" {
  description = "GitHub repository whose workflows publish the tenant's config bundle and push its images, through the account's GitHub OIDC provider. allowed_refs lists the full refs that may publish, where a trailing * matches a prefix. github_repository_ids selects GitHub's immutable subject form. pull_request_subject is the OIDC subject suffix of the repository's pull request jobs, pull_request for a job that names no environment, and creates a claims probe role those jobs assume to list one claim digest at a time. Null creates no publisher role."
  type = object({
    oidc_provider_arn = string
    github_repository = string
    allowed_refs      = optional(list(string), ["refs/heads/main"])
    github_repository_ids = optional(object({
      owner_id      = number
      repository_id = number
    }))
    pull_request_subject = optional(string)
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
  description = "The tenant's rebar ticket store. bucket_name names the bucket, set to an existing bucket's name to keep that bucket, and empty uses <name_prefix>-<tenant>-rebar-tickets-<account ID>. operators are the principals besides the worker that may assume its reader and writer roles, admins seed and empty it, and writer_github_oidc_trust lets a workflow push it, as the rebar-ticket-store module describes."
  type = object({
    bucket_name = optional(string, "")
    operators   = optional(list(string), [])
    admins      = optional(list(string), [])
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
