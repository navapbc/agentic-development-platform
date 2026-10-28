variable "create_secret_store" {
  description = "Create the secret store. False provisions nothing, for an account that manages its secrets elsewhere."
  type        = bool
  default     = false
}

variable "name_prefix" {
  description = "Prefix for every resource name and for the secret names, which take the form <name_prefix>/<secret>."
  type        = string
}

variable "region" {
  description = "Region of the secrets, the rotation functions, and the restart automation."
  type        = string
}

variable "secret_readers" {
  description = "IAM role ARNs allowed to read each secret, keyed by the secret keys the README lists. Pass the ECS task execution role for a secret that ECS injects. A secret with no readers denies GetSecretValue to every principal except its rotation function."
  type        = map(list(string))
  default     = {}
}

variable "generated_secret_versions" {
  description = "Generation of each Terraform-generated secret, keyed by secret key. Incrementing a value writes a new random value to that secret on the next apply. Missing keys are generation 1."
  type        = map(number)
  default     = {}
}

variable "recovery_window_days" {
  description = "Days a deleted secret stays recoverable. 0 deletes it at once, which only a disposable environment should use."
  type        = number
  default     = 30

  validation {
    condition     = var.recovery_window_days == 0 || (var.recovery_window_days >= 7 && var.recovery_window_days <= 30)
    error_message = "recovery_window_days must be 0 or between 7 and 30."
  }
}

variable "rotation_schedule_expression" {
  description = "Secrets Manager rotation schedule in UTC for the database and cache credentials. The default is the first Sunday of every third month at 06:00 UTC."
  type        = string
  default     = "cron(0 6 ? 1/3 SUN#1 *)"
}

variable "rotation_window" {
  description = "Length of the rotation window that opens at the scheduled time, such as 3h."
  type        = string
  default     = "3h"
}

variable "create_database_credentials" {
  description = "Create the per-service PostgreSQL credentials, their rotation function, and the role bootstrap. Requires the Data API on the cluster."
  type        = bool
  default     = false
}

variable "database_cluster_arn" {
  description = "ARN of the Aurora PostgreSQL cluster whose roles the function bootstraps and rotates."
  type        = string
  default     = ""
}

variable "database_master_secret_arn" {
  description = "ARN of the RDS-managed master user secret. The function runs the bootstrap and the password changes through the Data API as that user."
  type        = string
  default     = ""
}

variable "platform_database" {
  description = "Database that holds the platform schema."
  type        = string
  default     = "agent_platform"
}

variable "platform_schema" {
  description = "Schema the platform migrations create and the application role reads and writes."
  type        = string
  default     = "agent_platform"
}

variable "rotate_elasticache_user" {
  description = "Attach rotation to the ElastiCache RBAC user secret that the elasticache module creates."
  type        = bool
  default     = false
}

variable "elasticache_user_secret_arn" {
  description = "ARN of the elasticache module's user secret, which holds username, password, and user_arn."
  type        = string
  default     = ""
}

variable "elasticache_user_arn" {
  description = "ARN of the ElastiCache RBAC user whose password the function rotates."
  type        = string
  default     = ""
}

variable "vpc_id" {
  description = "VPC of the rotation functions."
  type        = string
  default     = ""
}

variable "subnet_ids" {
  description = "Subnets of the rotation functions, normally the management tier."
  type        = list(string)
  default     = []
}

variable "endpoints_security_group_id" {
  description = "Security group of the shared interface endpoints. The functions reach Secrets Manager, the RDS Data API, and ElastiCache only through them."
  type        = string
  default     = ""
}

variable "restart_services" {
  description = "ECS service ARNs to redeploy after a secret changes, keyed by secret key. A rule redeploys them on a completed rotation or on a PutSecretValue that does not stage a pending version. At most five services per secret."
  type        = map(list(string))
  default     = {}

  validation {
    condition     = alltrue([for services in values(var.restart_services) : length(services) <= 5])
    error_message = "restart_services allows at most five ECS services per secret."
  }
}

variable "log_retention_days" {
  description = "CloudWatch retention for the rotation function logs. Must be a value CloudWatch Logs accepts."
  type        = number
  default     = 365
}

variable "iam_path" {
  description = "Path for the IAM roles this module creates."
  type        = string
  default     = "/"
}

variable "permissions_boundary_arn" {
  description = "Permissions boundary for the IAM roles this module creates. Empty attaches none."
  type        = string
  default     = ""
}

variable "tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = {}
}
