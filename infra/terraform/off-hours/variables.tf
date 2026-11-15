variable "create_off_hours_schedule" {
  description = "Scale the listed ECS services to zero at sleep_schedule and restore them at wake_schedule."
  type        = bool
  default     = false
}

variable "name_prefix" {
  description = "Prefix for the schedules, documents, roles, and parameter."
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
  description = "Seconds the wake waits for the foundation services to run before it starts the others. At most 540."
  type        = number
  default     = 540

  validation {
    condition     = var.foundation_wait_seconds >= 0 && var.foundation_wait_seconds <= 540
    error_message = "foundation_wait_seconds must be between 0 and 540, under the 600-second step limit."
  }
}

variable "tags" {
  description = "Tags for every resource."
  type        = map(string)
  default     = {}
}
