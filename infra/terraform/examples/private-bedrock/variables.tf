variable "region" {
  description = "AWS region for all resources."
  type        = string
  default     = "us-east-1"
}

variable "name_prefix" {
  description = "Name prefix applied to created resources."
  type        = string
  default     = "agentic-platform"
}

variable "tags" {
  description = "Tags applied to all created resources."
  type        = map(string)
  default     = {}
}

variable "create_network" {
  description = "Create a private VPC and subnets. Set false to use an existing network and supply vpc_id, subnet_ids, and endpoint_allowed_cidrs."
  type        = bool
  default     = false
}

variable "vpc_cidr" {
  description = "CIDR for the VPC when create_network is true."
  type        = string
  default     = "10.0.0.0/16"
}

variable "new_subnet_cidrs" {
  description = "CIDRs for the private subnets to create when create_network is true. One subnet per CIDR."
  type        = list(string)
  default     = ["10.0.1.0/24"]
}

variable "availability_zones" {
  description = "AZs for the created subnets, aligned to new_subnet_cidrs. Empty selects the first available AZs in the region."
  type        = list(string)
  default     = []
}

variable "vpc_id" {
  description = "Existing VPC id. Required when create_network is false."
  type        = string
  default     = ""
}

variable "subnet_ids" {
  description = "Existing private subnet ids. Required when create_network is false."
  type        = list(string)
  default     = []
}

variable "endpoint_allowed_cidrs" {
  description = "CIDRs permitted to reach the Bedrock endpoint on 443. Required when create_network is false. Ignored when create_network is true, where the created subnet CIDRs are used."
  type        = list(string)
  default     = []
}

variable "route_table_ids" {
  description = "Route tables for the jumphost S3 gateway endpoint when create_network is false. Empty auto-discovers the route tables for subnet_ids."
  type        = list(string)
  default     = []
}

variable "create_cloudtrail" {
  description = "Create a single-region management CloudTrail trail and its log bucket. Set false when an organization trail already covers this account."
  type        = bool
  default     = false
}

variable "create_endpoints" {
  description = "Create the shared VPC endpoints. Set false when the account already has bedrock-runtime, ssmmessages, ECR, logs, and kms endpoints."
  type        = bool
  default     = true
}

variable "create_ecr_repository" {
  description = "Create the jumphost image repository. Set false to reuse an existing registry, and set existing_ecr_repository_url and existing_ecr_repository_arn."
  type        = bool
  default     = true
}

variable "jumphost_repository_name" {
  description = "ECR repository name for the jumphost-base image when create_ecr_repository is true."
  type        = string
  default     = "agentic-platform/jumphost-base"
}

variable "existing_endpoints_security_group_id" {
  description = "Endpoint security group id to use when create_endpoints is false. The jumphost task is allowed 443 egress to it."
  type        = string
  default     = ""
}

variable "existing_ecr_repository_url" {
  description = "ECR repository URL to use when create_ecr_repository is false, without a tag."
  type        = string
  default     = ""
}

variable "existing_ecr_repository_arn" {
  description = "ECR repository ARN to use when create_ecr_repository is false."
  type        = string
  default     = ""
}

variable "cloudtrail_name" {
  description = "Name of the CloudTrail trail when create_cloudtrail is true."
  type        = string
  default     = "agentic-platform-management"
}

variable "jumphost_name" {
  description = "Logical jumphost name, matching JUMPHOST_NAME in the platform .env."
  type        = string
  default     = "bedrock-jumphost"
}

variable "jumphost_image_tag" {
  description = "Tag of the jumphost-base image the task runs. Push this tag with inv publish-jumphost-image before connecting."
  type        = string
  default     = "v1"
}

variable "jumphost_session_duration_seconds" {
  description = "Default jumphost task TTL. The tunnel can override it per session."
  type        = number
  default     = 3600
}

variable "management_subnet_cidrs" {
  description = "CIDRs for the management tier subnets when create_network is true. The jumphost and administrative tasks run here."
  type        = list(string)
  default     = ["10.0.21.0/24"]
}

variable "data_subnet_cidrs" {
  description = "CIDRs for the data tier subnets when create_network is true. Aurora requires two subnets in different Availability Zones, so supply at least two."
  type        = list(string)
  default     = ["10.0.11.0/24", "10.0.12.0/24"]
}

variable "public_subnet_cidrs" {
  description = "CIDRs for the public tier subnets when create_network is true and an egress path is selected. Only the NAT instance or NAT Gateway runs here."
  type        = list(string)
  default     = ["10.0.31.0/24"]
}

variable "management_subnet_ids" {
  description = "Existing management tier subnets when create_network is false. Empty runs management workloads in subnet_ids."
  type        = list(string)
  default     = []
}

variable "management_cidr_blocks" {
  description = "CIDRs of the existing management tier when create_network is false. Admitted to the endpoints and the data tier."
  type        = list(string)
  default     = []
}

variable "data_subnet_ids" {
  description = "Existing data tier subnets spanning at least two Availability Zones, required when create_network is false and a data tier service is enabled."
  type        = list(string)
  default     = []
}

variable "egress_mode" {
  description = "Outbound internet path for the application and management tiers. none, nat_instance, or nat_gateway. Null selects nat_instance for a created network and none for an existing one."
  type        = string
  default     = null

  validation {
    condition     = var.egress_mode == null || contains(["none", "nat_instance", "nat_gateway"], coalesce(var.egress_mode, "none"))
    error_message = "egress_mode must be none, nat_instance, or nat_gateway."
  }
}

variable "create_dns_firewall" {
  description = "Filter VPC DNS with a Route 53 Resolver DNS Firewall allowlist. Null enables it for a created network and disables it for an existing one."
  type        = bool
  default     = null
}

variable "dns_firewall_default_action" {
  description = "Action for a query outside the allowlist. BLOCK enforces. ALERT resolves and logs, for a trial run."
  type        = string
  default     = "BLOCK"
}

variable "dns_firewall_additional_domains" {
  description = "Domains added to the DNS Firewall allowlist, such as the SMTP relay host."
  type        = list(string)
  default     = []
}

variable "access_log_bucket" {
  description = "Bucket that receives S3 server access logs from every bucket this root creates. The bootstrap root creates it and prints it as the access_log_bucket output."
  type        = string

  validation {
    condition     = length(trim(var.access_log_bucket, " ")) > 0
    error_message = "access_log_bucket must name the bucket that receives S3 server access logs."
  }
}

variable "kms_key_arn" {
  description = "Customer managed KMS key for the data tier, the NAT instance volume, and the log groups. Empty uses each service's AWS managed key."
  type        = string
  default     = ""
}

variable "create_aurora" {
  description = "Create the Aurora PostgreSQL cluster in the data tier."
  type        = bool
  default     = false
}

variable "aurora_instance_count" {
  description = "Aurora instances. 1 for evaluation. 2 or more adds a reader that a failover promotes."
  type        = number
  default     = 1
}

variable "aurora_serverless_max_capacity" {
  description = "Maximum Aurora Serverless v2 capacity in ACUs."
  type        = number
  default     = 4
}

variable "aurora_deletion_protection" {
  description = "Block deletion of the Aurora cluster. Set false before an intentional destroy."
  type        = bool
  default     = true
}

variable "aurora_skip_final_snapshot" {
  description = "Destroy the Aurora cluster without a final snapshot. Set true only in a disposable environment."
  type        = bool
  default     = false
}

variable "aurora_bedtime_tags" {
  description = "Tags an organization scheduler reads to stop the cluster off hours, such as CustodianRdsBedtime."
  type        = map(string)
  default     = {}
}

variable "create_cache" {
  description = "Create the ElastiCache for Valkey cache in the data tier."
  type        = bool
  default     = false
}

variable "cache_num_nodes" {
  description = "Cache nodes. 1 for evaluation."
  type        = number
  default     = 1
}

variable "create_clickhouse_storage" {
  description = "Create the encrypted EFS file system that backs the ClickHouse task."
  type        = bool
  default     = false
}

variable "create_langfuse_bucket" {
  description = "Create the S3 bucket for Langfuse event and media uploads."
  type        = bool
  default     = false
}

variable "create_secret_store" {
  description = "Create the secret store. With create_aurora it adds the per-service database credentials, the role bootstrap, and their rotation. With create_cache it rotates the cache user."
  type        = bool
  default     = false
}

variable "secret_readers" {
  description = "IAM role ARNs allowed to read each secret, keyed by secret key. See the secret-store module README."
  type        = map(list(string))
  default     = {}
}

variable "secret_restart_services" {
  description = "ECS service ARNs to redeploy after each secret changes, keyed by secret key."
  type        = map(list(string))
  default     = {}
}

variable "secret_recovery_window_days" {
  description = "Days a deleted secret stays recoverable. A secret name cannot be reused during the window, so set 0 in a disposable environment that is destroyed and applied again."
  type        = number
  default     = 30
}

variable "create_rebar_ticket_store" {
  description = "Create the S3 bucket, KMS key, and reader and writer roles for the rebar ticket store."
  type        = bool
  default     = false
}

variable "rebar_ticket_store_trusted_principals" {
  description = "IAM principal ARNs allowed to assume the ticket store reader and writer roles the module creates."
  type        = list(string)
  default     = []
}

variable "rebar_ticket_store_admin_principals" {
  description = "IAM principal ARNs allowed to seed the ticket store and to empty its bucket before a destroy."
  type        = list(string)
  default     = []
}

variable "rebar_ticket_store_existing_reader_role_arn" {
  description = "Existing role that fetches the ticket store. Set it with the writer role to skip creating both."
  type        = string
  default     = ""
}

variable "rebar_ticket_store_existing_writer_role_arn" {
  description = "Existing role that pushes the ticket store. Set it with the reader role to skip creating both."
  type        = string
  default     = ""
}
