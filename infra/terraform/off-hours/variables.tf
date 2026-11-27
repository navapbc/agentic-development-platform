variable "create_off_hours_schedule" {
  description = "Scale the listed ECS services and EKS deployments to zero at sleep_schedule, check that nothing holds a database connection, and restore them at wake_schedule."
  type        = bool
  default     = false
}

variable "name_prefix" {
  description = "Prefix for the schedules, function, roles, parameter, alert topic, dead-letter queue, and metric namespace."
  type        = string
}

variable "region" {
  description = "AWS region of the services."
  type        = string
}

variable "cluster_arns" {
  description = "ECS clusters of the listed services, where the sleep watches the stopping tasks."
  type        = list(string)
  default     = []
}

variable "foundation_services" {
  description = "ECS service ARNs the other services depend on, such as the Temporal server and ClickHouse. They stop after the others and start before them."
  type        = list(string)
  default     = []
}

variable "services" {
  description = "ECS service ARNs that stop first and start once the foundation services run."
  type        = list(string)
  default     = []
}

variable "health_checked_services" {
  description = "Foundation ECS service ARNs, such as the Temporal server, whose tasks must pass their container health check before the wake starts services and the EKS deployments. Each must also be in foundation_services."
  type        = list(string)
  default     = []

  validation {
    condition     = length(setsubtract(var.health_checked_services, var.foundation_services)) == 0
    error_message = "health_checked_services must be a subset of foundation_services."
  }
}

variable "eks_deployer" {
  description = "The eks-deployer task the off-hours function runs to scale and check the EKS deployments that connect to the database, and those deployments, each as namespace/name, a platform deployment name, tenant-workers, tenant-review-workers, or mcp-gateway. Null leaves EKS out."
  type = object({
    cluster_arn        = string
    task_family        = string
    container          = string
    subnet_ids         = list(string)
    security_group_ids = list(string)
    pass_role_arns     = list(string)
    log_group_name     = string
    deployments        = list(string)
  })
  default = null
}

variable "node_group_arns" {
  description = "EKS managed node groups the sleep scales to zero nodes after the check before the foundation, and the wake restores before the foundation services start."
  type        = list(string)
  default     = []
}

variable "database" {
  description = "Aurora cluster whose pg_stat_activity the checks read through the Data API, and the secret of the pg_monitor role they read it as. Null leaves the connection check out."
  type = object({
    cluster_arn        = string
    monitor_secret_arn = string
  })
  default = null
}

variable "foundation_database_users" {
  description = "Database users of the foundation services, such as temporal, whose connections the check before the foundation services stop allows. The check before the cluster is left to pause allows none."
  type        = list(string)
  default     = []
}

variable "timezone" {
  description = "IANA time zone of the two schedules."
  type        = string
  default     = "America/Chicago"
}

variable "sleep_schedule" {
  description = "EventBridge Scheduler expression that scales the services to zero, in timezone."
  type        = string
  default     = "cron(0 20 ? * * *)"
}

variable "wake_schedule" {
  description = "EventBridge Scheduler expression that restores the services, in timezone."
  type        = string
  default     = "cron(0 7 ? * MON-FRI *)"
}

variable "rotation_schedule_expression" {
  description = "The secret store's rotation schedule. When it rotates on a Sunday, wake_schedule must not wake on a Saturday or a Sunday, so a rotation finds the services at zero."
  type        = string
  default     = "cron(0 6 ? 1/3 SUN#1 *)"
}

variable "foundation_wait_seconds" {
  description = "Seconds the wake waits for the foundation services to run before it checks their health and starts the others. At most 540."
  type        = number
  default     = 540

  validation {
    condition     = var.foundation_wait_seconds >= 0 && var.foundation_wait_seconds <= 540
    error_message = "foundation_wait_seconds must be between 0 and 540, within the 600-second function timeout of the step."
  }
}

variable "log_retention_days" {
  description = "CloudWatch retention for the off-hours function logs. Must be a value CloudWatch Logs accepts."
  type        = number
  default     = 365
}

variable "tags" {
  description = "Tags for every resource."
  type        = map(string)
  default     = {}
}
