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
  description = "Existing data tier subnets spanning at least two Availability Zones, required when create_network is false and a data tier service is enabled. The Temporal server and admin-tools tasks may reach their CIDRs on the database port."
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
  description = "IAM role or user ARNs allowed to read each secret, keyed by secret key. See the secret-store module README."
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

variable "rebar_ticket_store_writer_oidc_trust" {
  description = "GitHub Actions OIDC trust for the ticket store writer role, which lets the reconcile-bridge workflow push the store without a stored AWS key. See the rebar-ticket-store module for the shape."
  type = object({
    provider_arn      = string
    subjects          = list(string)
    job_workflow_refs = optional(list(string), [])
    audience          = optional(string, "sts.amazonaws.com")
  })
  default = null
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

variable "create_ecs_cluster" {
  description = "Create the platform ECS cluster that the Temporal control plane, the platform services, and other teams' services run on."
  type        = bool
  default     = false
}

variable "ecs_deployer_passable_role_arns" {
  description = "Task and execution role ARNs, wildcards allowed, that the ECS cluster deployer policy may pass to tasks."
  type        = list(string)
  default     = []
}

variable "create_container_services" {
  description = "Create the platform container services on the platform ECS cluster, starting with the ECR repositories for their images."
  type        = bool
  default     = false
}

variable "container_internal_acm_certificate_arn" {
  description = "ACM certificate the internal load balancer presents, naming coordination, repo-intelligence, otel-collector, and langfuse under container_internal_domain. Empty creates no internal load balancer."
  type        = string
  default     = ""
}

variable "container_internal_domain" {
  description = "Private hosted zone that names the platform services behind the internal load balancer."
  type        = string
  default     = "platform.internal"
}

variable "container_internal_allowed_cidr_blocks" {
  description = "CIDRs allowed to reach the internal load balancer in addition to the members of its client security group."
  type        = list(string)
  default     = []
}

variable "container_platform_image" {
  description = "Platform image reference with its digest, as inv publish-platform-images prints it. Empty creates no platform service."
  type        = string
  default     = ""
}

variable "container_repo_intelligence_image" {
  description = "Repository-intelligence image reference with its digest. Empty creates no platform service."
  type        = string
  default     = ""
}

variable "container_environment" {
  description = "ENVIRONMENT the platform tasks report, which names their config/<environment> files."
  type        = string
  default     = "aws"
}

variable "container_config_dir" {
  description = "Directory outside the repository holding the environment's config files, such as tickets.yaml, which the tasks receive. Empty sends none."
  type        = string
  default     = ""
}

variable "container_platform_ca_file" {
  description = "PEM file of the CA that issued container_internal_acm_certificate_arn, which the platform clients trust beside the system roots."
  type        = string
  default     = ""
}

variable "container_service_counts" {
  description = "Tasks inv platform-deploy runs per platform service."
  type        = map(number)
  default = {
    workflow-worker   = 1
    review-worker     = 1
    coordination      = 1
    repo-intelligence = 1
    repo-index-worker = 1
  }
}

variable "container_use_fips_endpoints" {
  description = "Run the platform tasks with AWS_USE_FIPS_ENDPOINT=true and add the bedrock-runtime-fips interface endpoint they call."
  type        = bool
  default     = true
}

variable "container_https_proxy" {
  description = "HTTPS_PROXY for the platform tasks on networks whose organization supplies egress."
  type        = string
  default     = ""
}

variable "container_enable_execute_command" {
  description = "Allow ECS Exec into the platform tasks."
  type        = bool
  default     = false
}

variable "container_langfuse_web_image" {
  description = "Langfuse web image reference with its digest, as inv publish-platform-images prints it. Empty with any other Langfuse image empty runs no Langfuse or ClickHouse."
  type        = string
  default     = ""
}

variable "container_langfuse_worker_image" {
  description = "Langfuse worker image reference with its digest."
  type        = string
  default     = ""
}

variable "container_clickhouse_image" {
  description = "ClickHouse image reference with its digest."
  type        = string
  default     = ""
}

variable "container_tls_sidecar_image" {
  description = "TLS sidecar image reference with its digest, which fronts Langfuse web and issues the collector's certificate."
  type        = string
  default     = ""
}

variable "container_collector_image" {
  description = "Collector image reference with its digest, as inv publish-platform-images prints it. Set with container_tls_sidecar_image, it runs the collector and points the platform tasks at it."
  type        = string
  default     = ""
}

variable "container_collector_exports_enabled" {
  description = "Export the collector's traces to X-Ray, its metrics to a Prometheus workspace, and its generation spans to Langfuse, and add the xray and aps-workspaces interface endpoints. False runs the collector with only the debug exporter."
  type        = bool
  default     = true
}

variable "container_xray_encryption_enabled" {
  description = "Set the region's X-Ray encryption to the telemetry KMS key. It is one setting per account and region, so it changes X-Ray encryption for every X-Ray user in the region."
  type        = bool
  default     = false
}

variable "container_langfuse_admin_email" {
  description = "Email of the Langfuse administrator created on the first start, whose password is the langfuse_admin_password secret."
  type        = string
  default     = ""
}

variable "container_langfuse_public_url" {
  description = "URL operators open Langfuse under through inv langfuse-ui, set as NEXTAUTH_URL. Empty uses https://langfuse.<container_internal_domain>:8444."
  type        = string
  default     = ""
}

variable "container_langfuse_task_sizes" {
  description = "CPU units and memory in MiB of the clickhouse, langfuse-web, and langfuse-worker tasks. Null keeps the module defaults."
  type        = map(object({ cpu = number, memory = number }))
  default     = null
}

variable "create_temporal_control_plane" {
  description = "Create the Temporal control plane on the platform ECS cluster, starting with the ECR repositories for its images."
  type        = bool
  default     = false
}

variable "temporal_frontend_acm_certificate_arn" {
  description = "ACM certificate the Temporal NLB presents to clients. Empty serves the frontend certificate inv temporal-certs put writes to Secrets Manager."
  type        = string
  default     = ""
}

variable "temporal_internode_acm_certificate_arn" {
  description = "ACM certificate that tracks the Temporal internode certificate for expiry. The server reads the certificate from Secrets Manager."
  type        = string
  default     = ""
}

variable "temporal_frontend_server_name" {
  description = "Name on the Temporal frontend certificate, which clients verify the frontend against. With temporal_frontend_acm_certificate_arn, the name on that certificate."
  type        = string
  default     = "frontend.temporal.internal"
}

variable "temporal_internode_server_name" {
  description = "Name on the Temporal internode certificate."
  type        = string
  default     = "internode.temporal.internal"
}

variable "temporal_frontend_certificate_publicly_trusted" {
  description = "The certificate in temporal_frontend_acm_certificate_arn chains to a public root, so clients verify it against the system trust store instead of the Temporal CA secret."
  type        = bool
  default     = false
}

variable "temporal_fips_140_mode" {
  description = "Run the Temporal server, admin-tools, and UI processes in Go's FIPS 140-3 mode. Set true in a FedRAMP environment."
  type        = bool
  default     = false
}

variable "temporal_server_image" {
  description = "Temporal server image reference with its digest, as inv publish-temporal-images prints it. Empty creates no server."
  type        = string
  default     = ""
}

variable "temporal_admin_tools_image" {
  description = "Temporal admin-tools image reference with its digest."
  type        = string
  default     = ""
}

variable "temporal_ui_image" {
  description = "Temporal UI image reference with its digest. Empty creates no UI."
  type        = string
  default     = ""
}

variable "temporal_server_count" {
  description = "Temporal server tasks that inv temporal-deploy runs."
  type        = number
  default     = 1
}

variable "temporal_ui_acm_certificate_arn" {
  description = "ACM certificate the Temporal UI load balancer presents. Empty creates no UI."
  type        = string
  default     = ""
}

variable "temporal_ui_allowed_cidr_blocks" {
  description = "CIDRs, such as a VPN client range, allowed to reach the Temporal UI load balancer in addition to the jumphost task."
  type        = list(string)
  default     = []
}

variable "temporal_enable_execute_command" {
  description = "Allow ECS Exec into the Temporal tasks."
  type        = bool
  default     = false
}

variable "off_hours_enabled" {
  description = "Scale the platform and Temporal ECS services to zero at off_hours_sleep_schedule and restore them at off_hours_wake_schedule, so the Aurora cluster pauses overnight."
  type        = bool
  default     = false
}

variable "off_hours_timezone" {
  description = "IANA time zone of the off-hours schedules."
  type        = string
  default     = "America/Chicago"
}

variable "off_hours_sleep_schedule" {
  description = "EventBridge Scheduler expression that scales the services to zero."
  type        = string
  default     = "cron(0 20 ? * * *)"
}

variable "off_hours_wake_schedule" {
  description = "EventBridge Scheduler expression that restores the services. It must not wake on a weekend while the secret store rotates on a Sunday."
  type        = string
  default     = "cron(0 7 ? * MON-FRI *)"
}

variable "create_github_image_publisher" {
  description = "Create the role the publish-images GitHub Actions workflow assumes to push the platform images. Needs create_container_services."
  type        = bool
  default     = false
}

variable "github_oidc_provider_arn" {
  description = "ARN of the account's existing GitHub OIDC provider. Empty creates one, which fails when the account already holds a provider for token.actions.githubusercontent.com."
  type        = string
  default     = ""
}

variable "github_repository" {
  description = "The owner/name of the repository whose publish-images workflow may assume the role."
  type        = string
  default     = "navapbc/agentic-development-platform"
}

variable "github_repository_ids" {
  description = "Numeric owner and repository IDs of github_repository when its OIDC tokens carry GitHub's immutable subject. Null matches the name form."
  type = object({
    owner_id      = number
    repository_id = number
  })
  default = null
}

variable "github_publish_refs" {
  description = "The refs whose workflow runs may assume the publisher role."
  type        = list(string)
  default     = ["refs/heads/main", "refs/tags/v*"]
}
