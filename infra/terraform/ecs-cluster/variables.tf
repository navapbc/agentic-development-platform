variable "create_ecs_cluster" {
  description = "Create the platform ECS cluster, its KMS keys, its log groups, and the deployer and exec task policies."
  type        = bool
  default     = false
}

variable "name_prefix" {
  description = "Prefix for the cluster, key alias, log group, and policy names."
  type        = string
}

variable "cluster_name" {
  description = "Cluster name. Defaults to <name_prefix>-ecs."
  type        = string
  default     = ""
}

variable "container_insights" {
  description = "Container Insights level, enhanced for task and container metrics, enabled for cluster and service metrics, or disabled."
  type        = string
  default     = "enhanced"

  validation {
    condition     = contains(["enhanced", "enabled", "disabled"], var.container_insights)
    error_message = "container_insights must be enhanced, enabled, or disabled."
  }
}

variable "default_capacity_provider" {
  description = "Capacity provider a service or task uses when it names none. FARGATE_SPOT suits interruptible work only."
  type        = string
  default     = "FARGATE"

  validation {
    condition     = contains(["FARGATE", "FARGATE_SPOT"], var.default_capacity_provider)
    error_message = "default_capacity_provider must be FARGATE or FARGATE_SPOT."
  }
}

variable "log_retention_days" {
  description = "Retention for the ECS Exec and Container Insights log groups. Must be a value CloudWatch Logs accepts."
  type        = number
  default     = 365
}

variable "deployer_passable_role_arns" {
  description = "Task and execution role ARNs, wildcards allowed, that the deployer policy may pass to ECS tasks. A task definition that names a role outside this list is refused. Empty allows no role."
  type        = list(string)
  default     = []
}

variable "tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = {}
}
