variable "create_container_services" {
  description = "Create the platform container services and their image repositories."
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

variable "vpc_id" {
  description = "VPC the internal load balancer and its private hosted zone belong to."
  type        = string
  default     = ""
}

variable "application_subnet_ids" {
  description = "Application tier subnets in at least two Availability Zones, for the internal load balancer and the services."
  type        = list(string)
  default     = []
}

variable "internal_acm_certificate_arn" {
  description = "ACM certificate the internal load balancer presents, naming each internal service under internal_domain. Empty creates no internal load balancer."
  type        = string
  default     = ""

  validation {
    condition     = var.internal_acm_certificate_arn == "" || can(regex("^arn:aws[a-z-]*:acm:[a-z0-9-]+:[0-9]{12}:certificate/", var.internal_acm_certificate_arn))
    error_message = "internal_acm_certificate_arn must be an ACM certificate ARN."
  }
}

variable "internal_domain" {
  description = "Private hosted zone that names each internal service, such as coordination.<internal_domain>."
  type        = string
  default     = "platform.internal"
}

variable "internal_allowed_cidr_blocks" {
  description = "CIDRs allowed to reach the internal load balancer in addition to the members of its client security group."
  type        = list(string)
  default     = []
}

variable "access_log_bucket" {
  description = "Bucket that receives the S3 server access logs of the load balancer log bucket, such as the bucket the access-logs module creates."
  type        = string
  default     = ""
}

variable "load_balancer_log_retention_days" {
  description = "Days the internal load balancer access logs are kept."
  type        = number
  default     = 365
}

variable "cluster_arn" {
  description = "Platform ECS cluster the services and one-shot tasks run on."
  type        = string
  default     = ""
}

variable "management_subnet_ids" {
  description = "Management tier subnets for the one-shot db-migrate and schedule-bootstrap tasks."
  type        = list(string)
  default     = []
}

variable "data_subnet_ids" {
  description = "Data tier subnets for the mount targets of the workspace file system, one per Availability Zone the services use."
  type        = list(string)
  default     = []
}

variable "endpoints_security_group_id" {
  description = "Security group of the shared VPC interface endpoints, which every task reaches on 443."
  type        = string
  default     = ""
}

variable "platform_image" {
  description = "Platform image reference with its digest, as inv publish-platform-images prints it. Empty creates no service."
  type        = string
  default     = ""
}

variable "repo_intelligence_image" {
  description = "Repository-intelligence image reference with its digest. Empty creates no service."
  type        = string
  default     = ""
}

variable "environment" {
  description = "ENVIRONMENT the platform settings report, which names the config/<environment> files the tasks read."
  type        = string
  default     = "aws"
}

variable "environment_config" {
  description = "Config file name to YAML content, the operator's config/<environment> files, which the platform image writes under /tmp/platform-config at start. Holds no secret."
  type        = map(string)
  default     = {}
}

variable "tenant_workspaces" {
  description = "Tenants that receive their own workspace access point, rooted at /tenants/<tenant> on the workspace file system."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for tenant in var.tenant_workspaces : can(regex("^[a-z][a-z0-9]{1,15}$", tenant))])
    error_message = "A tenant name is 2 to 16 lowercase letters and digits, starting with a letter."
  }
}

variable "platform_settings" {
  description = "Platform-wide settings the platform tasks and the EKS workers receive in their environment, such as TICKET_BACKEND. A setting may not name a secret or a variable the module sets."
  type        = map(string)
  default     = {}

  validation {
    condition     = alltrue([for name in keys(var.platform_settings) : can(regex("^[A-Z][A-Z0-9_]*$", name)) && !can(regex("(KEY|TOKEN|SECRET|PASSWORD|PAT)", name))])
    error_message = "A platform setting name is an upper-case variable name and may not contain KEY, TOKEN, SECRET, PASSWORD, or PAT, because a secret belongs in Secrets Manager."
  }
}

variable "platform_ca_pem" {
  description = "PEM of the CA that issued internal_acm_certificate_arn, which the platform clients trust beside the system roots. Empty when that certificate chains to a public root."
  type        = string
  default     = ""
}

variable "log_level" {
  description = "LOG_LEVEL of every platform task."
  type        = string
  default     = "INFO"
}

variable "log_retention_days" {
  description = "Days the task logs are kept."
  type        = number
  default     = 365
}

variable "database_host" {
  description = "Aurora cluster endpoint the platform connects to."
  type        = string
  default     = ""
}

variable "database_port" {
  description = "Aurora port."
  type        = number
  default     = 5432
}

variable "database_name" {
  description = "Platform database name."
  type        = string
  default     = "agent_platform"
}

variable "database_cidr_blocks" {
  description = "Data tier CIDRs the tasks reach on the database port."
  type        = list(string)
  default     = []
}

variable "database_pool_size" {
  description = "Connections each database engine keeps open, DATABASE_POOL_SIZE. Every service holds several engines, so the README sizes this against max_connections."
  type        = number
  default     = 2
}

variable "database_pool_max_overflow" {
  description = "Connections each engine opens beyond database_pool_size under load, DATABASE_POOL_MAX_OVERFLOW."
  type        = number
  default     = 3
}

variable "database_app_secret_arn" {
  description = "Secret store secret of the platform_app database role, with username and password keys."
  type        = string
  default     = ""
}

variable "database_migrator_secret_arn" {
  description = "Secret store secret of the platform_migrator database role, which db-migrate runs as."
  type        = string
  default     = ""
}

variable "secret_arns" {
  description = "Secret store secrets by key. coordination_shared_secret, review_signing_key, and review_attestation_secret are required."
  type        = map(string)
  default     = {}
}

variable "secret_kms_key_arn" {
  description = "KMS key that encrypts the secret store secrets."
  type        = string
  default     = ""
}

variable "temporal" {
  description = "Temporal client settings from the temporal-control-plane module: endpoint, namespace, server_name, ca_secret_arn (null when the frontend chains to a public root), ca_kms_key_arn, and client_security_group_id."
  type = object({
    endpoint                 = string
    namespace                = string
    server_name              = string
    ca_secret_arn            = optional(string)
    ca_kms_key_arn           = optional(string)
    client_security_group_id = string
  })
  default = null
}

variable "bedrock_invoke_policy_arns" {
  description = "Policies granting bedrock:InvokeModel, attached to the review worker task role only. A list, so the count is known before the policies exist."
  type        = list(string)
  default     = []
}

variable "evidence_bucket_arn" {
  description = "Evidence store bucket the review worker writes to. Empty keeps evidence in Postgres only."
  type        = string
  default     = ""
}

variable "evidence_kms_key_arn" {
  description = "KMS key of the evidence store bucket, when it uses SSE-KMS."
  type        = string
  default     = ""
}

variable "service_counts" {
  description = "Tasks inv platform-deploy runs per service."
  type        = map(number)
  default = {
    workflow-worker   = 1
    review-worker     = 1
    coordination      = 1
    repo-intelligence = 1
    repo-index-worker = 1
  }
}

variable "service_environment" {
  description = "Extra environment values per service, keyed by service name, merged over the module's values."
  type        = map(map(string))
  default     = {}
}

variable "https_proxy" {
  description = "HTTPS_PROXY for networks whose organization supplies egress. Empty uses the VPC's NAT path."
  type        = string
  default     = ""
}

variable "no_proxy" {
  description = "NO_PROXY with https_proxy. The module adds the internal domain and the AWS service names."
  type        = string
  default     = ""
}

variable "enable_execute_command" {
  description = "Allow ECS Exec into the platform tasks."
  type        = bool
  default     = false
}

variable "exec_task_policy_arn" {
  description = "Exec task policy from the ecs-cluster module, attached to the task roles when enable_execute_command is true."
  type        = string
  default     = ""
}

variable "use_fips_endpoints" {
  description = "Set AWS_USE_FIPS_ENDPOINT=true on every task, which needs the FIPS interface endpoints of the AWS services the tasks call, such as bedrock-runtime-fips."
  type        = bool
  default     = true
}

variable "collector_image" {
  description = "Collector image reference with its digest. Set, it runs the collector behind the internal load balancer and points every platform task's OTLP exporters at it."
  type        = string
  default     = ""
}

variable "collector_exports_enabled" {
  description = "Export the collector's traces to X-Ray, its metrics to a Prometheus workspace the module creates, and its generation spans to Langfuse. False runs the collector with only the debug exporter, for a deployment that sends telemetry elsewhere."
  type        = bool
  default     = true
}

variable "xray_encryption_enabled" {
  description = "Set the region's X-Ray encryption to the telemetry KMS key. X-Ray encryption is one setting per account and region, so this changes it for every X-Ray user in the region."
  type        = bool
  default     = false
}

variable "tls_sidecar_image" {
  description = "TLS sidecar image reference with its digest, which fronts Langfuse web and issues the collector's certificate."
  type        = string
  default     = ""
}

variable "langfuse" {
  description = "Langfuse and ClickHouse images, the cache, the ClickHouse file system, the blob bucket, the URL operators open, and the administrator. Null runs neither."
  type = object({
    web_image        = string
    worker_image     = string
    clickhouse_image = string
    public_url       = string
    admin_email      = string
    cache = object({
      endpoint          = string
      port              = number
      username          = string
      security_group_id = string
    })
    clickhouse_storage = object({
      file_system_id    = string
      file_system_arn   = string
      access_point_id   = string
      access_point_arn  = string
      security_group_id = string
    })
    bucket = object({
      name        = string
      arn         = string
      kms_key_arn = string
    })
  })
  default = null
}

variable "langfuse_cache_secret_arn" {
  description = "Secret of the cache RBAC user Langfuse authenticates as, whose password field becomes REDIS_AUTH. A separate input from langfuse, because the secret store key policy that encrypts it lists the roles this module creates."
  type        = string
  default     = ""
}

variable "langfuse_task_sizes" {
  description = "CPU units and memory in MiB of each Langfuse and ClickHouse task."
  type        = map(object({ cpu = number, memory = number }))
  nullable    = false
  default = {
    clickhouse      = { cpu = 2048, memory = 8192 }
    langfuse-web    = { cpu = 1024, memory = 2048 }
    langfuse-worker = { cpu = 1024, memory = 2048 }
  }
}

variable "clickhouse_namespace" {
  description = "Cloud Map private DNS namespace that names the ClickHouse task. Empty uses data.<internal_domain>."
  type        = string
  default     = ""
}

variable "secret_recovery_window_days" {
  description = "Days the deleted ClickHouse TLS secret stays recoverable. Set 0 in a disposable environment that is destroyed and applied again."
  type        = number
  default     = 30

  validation {
    condition     = var.secret_recovery_window_days == 0 || (var.secret_recovery_window_days >= 7 && var.secret_recovery_window_days <= 30)
    error_message = "secret_recovery_window_days must be 0 or between 7 and 30."
  }
}

variable "workspace_client_security_group_ids" {
  description = "Security groups outside the module allowed to mount the workspace file system on 2049, such as the EKS cluster security group, which the nodes that mount it for their pods carry."
  type        = list(string)
  default     = []
}

variable "internal_operator_security_group_ids" {
  description = "Security groups, such as the jumphost task group, allowed to reach the internal load balancer on 443. The module adds the matching egress rule to each group."
  type        = list(string)
  default     = []
}
