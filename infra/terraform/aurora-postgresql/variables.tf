variable "create_cluster" {
  description = "Master toggle. When false the module provisions nothing, so an existing database can be reused."
  type        = bool
  default     = false
}

variable "name_prefix" {
  description = "Prefix for the cluster, instance, and supporting resource names."
  type        = string
}

variable "vpc_id" {
  description = "VPC the cluster runs in."
  type        = string
  default     = ""
}

variable "subnet_ids" {
  description = "Data-tier subnets for the DB subnet group. Aurora requires subnets in at least two Availability Zones, even for one instance."
  type        = list(string)
  default     = []
}

variable "ingress_security_group_ids" {
  description = "Security groups admitted to the database port."
  type        = list(string)
  default     = []
}

variable "ingress_cidr_blocks" {
  description = "CIDRs admitted to the database port. Pass the application and management tier CIDRs."
  type        = list(string)
  default     = []
}

variable "engine_version" {
  description = "Aurora PostgreSQL engine version. Automatic minor upgrades move a running cluster forward, and Terraform ignores that drift."
  type        = string
  default     = "16.15"
}

variable "auto_minor_version_upgrade" {
  description = "Apply new minor versions in the maintenance window, which carry the back-ported security fixes."
  type        = bool
  default     = true
}

variable "instance_count" {
  description = "Number of cluster instances. One writer is the evaluation default. Set 2 or more for a reader that a failover promotes."
  type        = number
  default     = 1

  validation {
    condition     = var.instance_count >= 1
    error_message = "instance_count must be at least 1."
  }
}

variable "instance_class" {
  description = "Instance class. db.serverless uses the Serverless v2 capacity range below."
  type        = string
  default     = "db.serverless"
}

variable "serverless_min_capacity" {
  description = "Minimum Serverless v2 capacity in ACUs. 0 lets the cluster pause after seconds_until_auto_pause with no connections."
  type        = number
  default     = 0
}

variable "serverless_max_capacity" {
  description = "Maximum Serverless v2 capacity in ACUs. It also sets the max_connections ceiling."
  type        = number
  default     = 4
}

variable "seconds_until_auto_pause" {
  description = "Idle seconds with no user connections before a cluster at 0 ACU pauses."
  type        = number
  default     = 3600

  validation {
    condition     = var.seconds_until_auto_pause >= 300 && var.seconds_until_auto_pause <= 86400
    error_message = "seconds_until_auto_pause must be between 300 and 86400."
  }
}

variable "database_name" {
  description = "Initial database created with the cluster."
  type        = string
  default     = "agent_platform"
}

variable "master_username" {
  description = "Master user name. The password is generated and rotated by RDS in Secrets Manager. Services connect as their own roles, not as this user."
  type        = string
  default     = "platform_admin"
}

variable "port" {
  description = "Database port."
  type        = number
  default     = 5432
}

variable "kms_key_arn" {
  description = "KMS key ARN for storage encryption. Empty uses the AWS managed aws/rds key."
  type        = string
  default     = ""
}

variable "master_user_secret_kms_key_arn" {
  description = "KMS key ARN for the master user secret. Empty uses the AWS managed aws/secretsmanager key."
  type        = string
  default     = ""
}

variable "backup_retention_days" {
  description = "Automated backup retention in days."
  type        = number
  default     = 30

  validation {
    condition     = var.backup_retention_days >= 1 && var.backup_retention_days <= 35
    error_message = "backup_retention_days must be between 1 and 35."
  }
}

variable "preferred_backup_window" {
  description = "Daily UTC backup window such as 05:00-06:00. Null lets RDS choose."
  type        = string
  default     = null
}

variable "preferred_maintenance_window" {
  description = "Weekly UTC maintenance window such as sun:06:00-sun:07:00. Null lets RDS choose."
  type        = string
  default     = null
}

variable "deletion_protection" {
  description = "Block cluster deletion. Set false before an intentional destroy."
  type        = bool
  default     = true
}

variable "log_retention_days" {
  description = "CloudWatch retention for the exported PostgreSQL log. Must be a value CloudWatch Logs accepts."
  type        = number
  default     = 365
}

variable "log_kms_key_arn" {
  description = "KMS key ARN for the exported log group. Empty uses the CloudWatch Logs default encryption."
  type        = string
  default     = ""
}

variable "ca_cert_identifier" {
  description = "Certificate authority for the instance server certificate. Clients verify against this CA."
  type        = string
  default     = "rds-ca-rsa2048-g1"
}

variable "performance_insights_enabled" {
  description = "Enable Performance Insights on each instance."
  type        = bool
  default     = false
}

variable "cluster_parameters" {
  description = "Cluster parameters merged over the defaults, keyed by name with the value and apply method."
  type = map(object({
    value        = string
    apply_method = optional(string, "immediate")
  }))
  default = {}
}

variable "bedtime_tags" {
  description = "Tags an organization scheduler reads to stop the cluster off hours, such as CustodianRdsBedtime. Empty adds none."
  type        = map(string)
  default     = {}
}

variable "tags" {
  description = "Tags applied to the created resources."
  type        = map(string)
  default     = {}
}
