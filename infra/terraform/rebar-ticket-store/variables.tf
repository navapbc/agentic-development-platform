variable "create_rebar_ticket_store" {
  description = "Create the rebar ticket store bucket, its KMS key, and, unless existing role ARNs are set, its reader and writer roles."
  type        = bool
  default     = false
}

variable "name_prefix" {
  description = "Prefix for the bucket, key alias, and role names."
  type        = string
}

variable "bucket_name" {
  description = "Name of the bucket. Set it to the name of an existing store bucket to keep that bucket, such as one created with a generated name. Empty uses <bucket_prefix><account ID>."
  type        = string
  default     = ""

  validation {
    condition     = var.bucket_name == "" || can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.bucket_name))
    error_message = "bucket_name must be a valid S3 bucket name of 3 to 63 lowercase letters, digits, dots, and hyphens."
  }
}

variable "bucket_prefix" {
  description = "Prefix of the default bucket name, which the account ID completes. Empty uses <name_prefix>-rebar-tickets-. Ignored when bucket_name is set."
  type        = string
  default     = ""
}

variable "prefix" {
  description = "Key prefix the ticket store syncs under, the path part of its s3:// remote URL."
  type        = string
  default     = "tickets"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9._-]*$", var.prefix))
    error_message = "prefix must be one lowercase path segment without slashes."
  }
}

variable "trusted_principals" {
  description = "IAM principal ARNs allowed to assume the created reader and writer roles, such as the platform task roles or an operator. Required when the module creates the roles."
  type        = list(string)
  default     = []
}

variable "trusted_principals_by_condition" {
  description = "IAM role or user ARNs in this account allowed to assume the created reader and writer roles through an aws:PrincipalArn condition on the account principal. Unlike trusted_principals, IAM does not require the principal to exist, so the trust creates no Terraform dependency on it and holds when the principal is deleted and recreated under the same name. Each principal needs an identity policy that allows sts:AssumeRole on the roles."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for arn in var.trusted_principals_by_condition : can(regex("^arn:[^:]+:iam::[0-9]{12}:(role|user)/[^*?]+$", arn))])
    error_message = "trusted_principals_by_condition must hold IAM role or user ARNs without wildcards, because ArnEquals expands * and ? and would widen the trust."
  }
}

variable "writer_github_oidc_trust" {
  description = "GitHub Actions OIDC trust for the writer role, which lets a workflow push the store without a stored AWS key. provider_arn names the IAM OIDC provider, and the trust policy's condition keys follow the issuer host it carries, so a GitHub Enterprise Server provider works as well as token.actions.githubusercontent.com. subjects lists the fully qualified claims permitted to assume the role, such as repo:OWNER/NAME:ref:refs/heads/main. job_workflow_refs restricts which workflow file may do so. Both claims are matched with StringEquals, so a wildcard is compared literally and is rejected. Leave it null and the writer trusts only trusted_principals."
  type = object({
    provider_arn      = string
    subjects          = list(string)
    job_workflow_refs = optional(list(string), [])
    audience          = optional(string, "sts.amazonaws.com")
  })
  default = null
}

variable "existing_reader_role_arn" {
  description = "ARN of an existing role that fetches the store. Set it together with existing_writer_role_arn to supply both roles, and the module then creates none."
  type        = string
  default     = ""
}

variable "existing_writer_role_arn" {
  description = "ARN of an existing role that pushes the store. Set it together with existing_reader_role_arn."
  type        = string
  default     = ""
}

variable "admin_principals" {
  description = "IAM principal ARNs allowed to read, write, and delete objects, for seeding the store and emptying the bucket before a destroy. Every other principal except the reader and writer is denied object access."
  type        = list(string)
  default     = []
}

variable "noncurrent_version_retention_days" {
  description = "Days a replaced bundle version stays restorable. Every push replaces the previous bundle, so this bounds how far back a bad push can be undone."
  type        = number
  default     = 90

  validation {
    condition     = var.noncurrent_version_retention_days >= 1
    error_message = "noncurrent_version_retention_days must be at least 1."
  }
}

variable "access_log_bucket" {
  description = "Target bucket for S3 server access logs, such as the bucket the access-logs module creates."
  type        = string

  validation {
    condition     = length(trim(var.access_log_bucket, " ")) > 0
    error_message = "access_log_bucket must name the bucket that receives S3 server access logs."
  }
}

variable "iam_path" {
  description = "Path for the created roles."
  type        = string
  default     = "/"
}

variable "permissions_boundary_arn" {
  description = "Permissions boundary for the created roles. Empty attaches none."
  type        = string
  default     = ""
}

variable "tags" {
  description = "Tags applied to the created resources."
  type        = map(string)
  default     = {}
}
