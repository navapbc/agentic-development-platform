variable "create_temporal_control_plane" {
  description = "Create the Temporal control plane, starting with the ECR repositories for the server and admin-tools images."
  type        = bool
  default     = false
}

variable "name_prefix" {
  description = "Prefix for the repository and resource names."
  type        = string
}

variable "kms_key_arn" {
  description = "Customer-managed KMS key ARN that encrypts the image repositories. Empty creates one key per repository."
  type        = string
  default     = ""
}

variable "tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = {}
}

variable "frontend_acm_certificate_arn" {
  description = "ACM certificate the NLB presents to Temporal clients, issued by ACM or imported into it. Empty serves the frontend certificate that inv temporal-certs put writes to Secrets Manager."
  type        = string
  default     = ""

  validation {
    condition     = var.frontend_acm_certificate_arn == "" || can(regex("^arn:aws[a-z-]*:acm:[a-z0-9-]+:[0-9]{12}:certificate/[0-9a-f-]+$", var.frontend_acm_certificate_arn))
    error_message = "frontend_acm_certificate_arn must be empty or an ACM certificate ARN."
  }
}

variable "internode_acm_certificate_arn" {
  description = "ACM certificate that tracks the internode certificate, imported by inv temporal-certs put --import-to-acm or by the operator. The server reads the internode certificate from Secrets Manager, because ACM never returns the key of an imported certificate, and put refuses a certificate that does not match this one."
  type        = string
  default     = ""

  validation {
    condition     = var.internode_acm_certificate_arn == "" || can(regex("^arn:aws[a-z-]*:acm:[a-z0-9-]+:[0-9]{12}:certificate/[0-9a-f-]+$", var.internode_acm_certificate_arn))
    error_message = "internode_acm_certificate_arn must be empty or an ACM certificate ARN."
  }
}

variable "tls_secret_readers" {
  description = "IAM role ARNs allowed to read the TLS secrets. Every other principal is denied GetSecretValue."
  type        = list(string)
  default     = []
}

variable "secret_recovery_window_days" {
  description = "Days a deleted TLS secret stays recoverable. Set 0 in a disposable environment that is destroyed and applied again."
  type        = number
  default     = 30

  validation {
    condition     = var.secret_recovery_window_days == 0 || (var.secret_recovery_window_days >= 7 && var.secret_recovery_window_days <= 30)
    error_message = "secret_recovery_window_days must be 0 or between 7 and 30."
  }
}

variable "cluster_arn" {
  description = "Platform ECS cluster the server, the admin-tools tasks, and the UI run on."
  type        = string
  default     = ""
}

variable "vpc_id" {
  description = "VPC of the control plane."
  type        = string
  default     = ""
}

variable "application_subnet_ids" {
  description = "Application tier subnets of the server and its internal NLB."
  type        = list(string)
  default     = []
}

variable "management_subnet_ids" {
  description = "Management tier subnets of the admin-tools tasks, the UI, and its internal ALB. The ALB needs two in different Availability Zones."
  type        = list(string)
  default     = []
}

variable "endpoints_security_group_id" {
  description = "Security group of the shared VPC interface endpoints the tasks reach ECR, CloudWatch Logs, Secrets Manager, and KMS through."
  type        = string
  default     = ""
}

variable "database_host" {
  description = "Aurora cluster endpoint that holds the temporal and temporal_visibility databases."
  type        = string
  default     = ""
}

variable "database_port" {
  description = "Aurora port."
  type        = number
  default     = 5432
}

variable "database_cidr_blocks" {
  description = "Data tier CIDRs the server and the schema task reach Aurora in."
  type        = list(string)
  default     = []
}

variable "database_secret_arn" {
  description = "Secret holding the username and password of the temporal database role, the secret-store database_temporal secret."
  type        = string
  default     = ""
}

variable "database_secret_kms_key_arn" {
  description = "KMS key that encrypts database_secret_arn."
  type        = string
  default     = ""
}

variable "server_image" {
  description = "Server image reference with its digest, as inv publish-temporal-images prints it. Empty creates no task or service."
  type        = string
  default     = ""
}

variable "admin_tools_image" {
  description = "Admin-tools image reference with its digest. Empty creates no task or service."
  type        = string
  default     = ""
}

variable "ui_image" {
  description = "UI image reference with its digest. Empty creates no UI."
  type        = string
  default     = ""
}

variable "server_count" {
  description = "Server tasks that inv temporal-deploy runs. Each runs every Temporal service."
  type        = number
  default     = 1
}

variable "server_cpu" {
  description = "CPU units of a server task."
  type        = number
  default     = 1024
}

variable "server_memory" {
  description = "Memory in MiB of a server task."
  type        = number
  default     = 2048
}

variable "num_history_shards" {
  description = "History shards. Temporal fixes the count when the cluster first starts and refuses a change afterwards."
  type        = number
  default     = 512
}

variable "namespace" {
  description = "Namespace the admin-tools task registers and the workers use."
  type        = string
  default     = "default"
}

variable "namespace_retention" {
  description = "Retention of closed workflow histories in the namespace."
  type        = string
  default     = "720h"
}

variable "frontend_server_name" {
  description = "Name on the frontend certificate, which clients verify the frontend against. With an ACM frontend, the name on that certificate."
  type        = string
  default     = "frontend.temporal.internal"
}

variable "internode_server_name" {
  description = "Name on the internode certificate."
  type        = string
  default     = "internode.temporal.internal"
}

variable "frontend_certificate_publicly_trusted" {
  description = "The ACM frontend certificate chains to a public root, so clients verify it against the system trust store instead of the CA secret."
  type        = bool
  default     = false
}

variable "dynamic_config" {
  description = "Temporal dynamic configuration, keyed by setting name, each a list of value and constraints entries as Temporal's file format takes them. Worker Versioning V1 keys are refused."
  type        = any
  default     = {}

  validation {
    condition = length(setintersection(keys(var.dynamic_config), [
      "frontend.workerVersioningDataAPIs",
      "frontend.workerVersioningWorkflowAPIs",
      "limit.versionCompatibleSetLimitPerQueue",
      "limit.versionBuildIdLimitPerQueue",
    ])) == 0
    error_message = "dynamic_config sets a Worker Versioning V1 key, which server 1.33 removes. The workers run on Worker Deployments."
  }
}

variable "ui_acm_certificate_arn" {
  description = "ACM certificate the UI load balancer presents. Empty creates no UI."
  type        = string
  default     = ""
}

variable "ui_allowed_cidr_blocks" {
  description = "CIDRs allowed to reach the UI load balancer on 443, such as a VPN client range."
  type        = list(string)
  default     = []
}

variable "ui_operator_security_group_ids" {
  description = "Security groups, such as the jumphost task group, allowed to reach the UI load balancer on 443. The module adds the matching egress rule to each group."
  type        = list(string)
  default     = []
}

variable "enable_execute_command" {
  description = "Allow ECS Exec into the server, admin-tools, and UI tasks. Needs exec_task_policy_arn."
  type        = bool
  default     = false
}

variable "exec_task_policy_arn" {
  description = "Policy from the ecs-cluster module that lets a task accept ECS Exec sessions."
  type        = string
  default     = ""
}

variable "log_retention_days" {
  description = "Retention of the server, admin-tools, and UI log groups. Must be a value CloudWatch Logs accepts."
  type        = number
  default     = 365
}

variable "access_log_bucket" {
  description = "Bucket that receives the S3 server access logs of the load balancer log bucket, such as the bucket the access-logs module creates."
  type        = string
  default     = ""
}

variable "load_balancer_log_retention_days" {
  description = "Days the NLB and ALB access logs are kept."
  type        = number
  default     = 365
}
