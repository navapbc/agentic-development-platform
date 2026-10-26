variable "create_cache" {
  description = "Master toggle. When false the module provisions nothing, so an existing cache can be reused."
  type        = bool
  default     = false
}

variable "name_prefix" {
  description = "Prefix for the replication group, user, and supporting resource names. Keep it short, because the replication group id allows 40 characters."
  type        = string
}

variable "vpc_id" {
  description = "VPC the cache runs in."
  type        = string
  default     = ""
}

variable "subnet_ids" {
  description = "Data-tier subnets for the cache subnet group."
  type        = list(string)
  default     = []
}

variable "ingress_security_group_ids" {
  description = "Security groups admitted to the cache port."
  type        = list(string)
  default     = []
}

variable "ingress_cidr_blocks" {
  description = "CIDRs admitted to the cache port. Pass the application tier CIDRs."
  type        = list(string)
  default     = []
}

variable "engine_version" {
  description = "Valkey engine version."
  type        = string
  default     = "9.1"
}

variable "node_type" {
  description = "Cache node type."
  type        = string
  default     = "cache.t4g.micro"
}

variable "num_cache_clusters" {
  description = "Number of nodes. One is the evaluation default. Set 2 or more with automatic_failover_enabled for a replica in another Availability Zone."
  type        = number
  default     = 1

  validation {
    condition     = var.num_cache_clusters >= 1
    error_message = "num_cache_clusters must be at least 1."
  }
}

variable "automatic_failover_enabled" {
  description = "Promote a replica when the primary fails. Requires num_cache_clusters of 2 or more."
  type        = bool
  default     = false
}

variable "multi_az_enabled" {
  description = "Place the replica in another Availability Zone. Requires automatic_failover_enabled."
  type        = bool
  default     = false
}

variable "port" {
  description = "Cache port."
  type        = number
  default     = 6379
}

variable "maxmemory_policy" {
  description = "Eviction policy. Langfuse requires noeviction so queued BullMQ jobs are never evicted."
  type        = string
  default     = "noeviction"
}

variable "username" {
  description = "RBAC user the client authenticates as."
  type        = string
  default     = "langfuse"
}

variable "access_string" {
  description = "RBAC access string for the user. Langfuse requires access to all keys and commands."
  type        = string
  default     = "on ~* +@all"
}

variable "kms_key_arn" {
  description = "KMS key ARN for at-rest encryption. Empty uses the AWS managed key."
  type        = string
  default     = ""
}

variable "secret_kms_key_arn" {
  description = "KMS key ARN for the user credential secret. Empty uses the AWS managed aws/secretsmanager key."
  type        = string
  default     = ""
}

variable "snapshot_retention_days" {
  description = "Days of automatic snapshots kept. 0 disables snapshots."
  type        = number
  default     = 1
}

variable "snapshot_window" {
  description = "Daily UTC snapshot window such as 05:00-06:00. Null lets ElastiCache choose."
  type        = string
  default     = null
}

variable "maintenance_window" {
  description = "Weekly UTC maintenance window such as sun:06:00-sun:07:00. Null lets ElastiCache choose."
  type        = string
  default     = null
}

variable "log_retention_days" {
  description = "CloudWatch retention for the engine log. Must be a value CloudWatch Logs accepts."
  type        = number
  default     = 365
}

variable "log_kms_key_arn" {
  description = "KMS key ARN for the engine log group. Empty uses the CloudWatch Logs default encryption."
  type        = string
  default     = ""
}

variable "tags" {
  description = "Tags applied to the created resources."
  type        = map(string)
  default     = {}
}
