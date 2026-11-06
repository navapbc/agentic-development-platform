output "vpc_id" {
  description = "VPC the resources run in, created or supplied."
  value       = local.vpc_id
}

output "subnet_ids" {
  description = "Private subnets the resources run in, created or supplied."
  value       = local.subnet_ids
}

output "jumphost_ecr_repository_url" {
  description = "Repository to push the jumphost image to before connecting."
  value       = module.jumphost_ecr.repository_url
}

output "jumphost_cluster_name" {
  description = "ECS cluster the connect script runs a task on."
  value       = module.bedrock_jumphost.cluster_name
}

output "jumphost_ssm_param_prefix" {
  description = "SSM parameter prefix the connect script resolves."
  value       = module.bedrock_jumphost.ssm_param_prefix
}

output "bedrock_invoke_policy_arn" {
  description = "Attach this to the workload role that signs Bedrock inference."
  value       = module.bedrock_iam.invoke_policy_arn
}

output "vpc_endpoints_security_group_id" {
  description = "Shared endpoint security group id."
  value       = module.vpc_endpoints.security_group_id
}

output "management_subnet_ids" {
  description = "Management tier subnets, where the jumphost and administrative tasks run."
  value       = local.management_subnet_ids
}

output "data_subnet_ids" {
  description = "Data tier subnets. They have no default route and a network ACL that admits only the application and management tiers."
  value       = local.data_subnet_ids
}

output "egress_mode" {
  description = "Outbound internet path in effect."
  value       = module.egress.egress_mode
}

output "egress_public_ip" {
  description = "Address outbound traffic appears from, for destinations that filter by source address."
  value       = module.egress.public_ip
}

output "dns_firewall_allowed_domains" {
  description = "Domains the VPC may resolve when the DNS Firewall is enabled."
  value       = module.dns_firewall.allowed_domains
}

output "aurora_endpoint" {
  description = "Aurora writer endpoint, or null when create_aurora is false."
  value       = module.aurora_postgresql.endpoint
}

output "aurora_master_user_secret_arn" {
  description = "RDS-managed master user secret. Reserve it for administration and role bootstrap."
  value       = module.aurora_postgresql.master_user_secret_arn
}

output "cache_endpoint" {
  description = "Valkey primary endpoint, or null when create_cache is false."
  value       = module.elasticache.primary_endpoint_address
}

output "cache_user_secret_arn" {
  description = "Secret holding the Valkey RBAC user credential for Langfuse."
  value       = module.elasticache.user_secret_arn
}

output "clickhouse_file_system_id" {
  description = "EFS file system backing the ClickHouse task, or null when create_clickhouse_storage is false."
  value       = module.clickhouse_storage.file_system_id
}

output "clickhouse_access_point_id" {
  description = "EFS access point the ClickHouse task mounts."
  value       = module.clickhouse_storage.access_point_id
}

output "langfuse_bucket" {
  description = "Langfuse event and media bucket, or null when create_langfuse_bucket is false."
  value       = module.langfuse_blob_store.bucket_id
}

output "secret_arns" {
  description = "Platform secret ARNs keyed by secret key, empty when create_secret_store is false."
  value       = module.secret_store.secret_arns
}

output "database_secret_arns" {
  description = "Per-service database credential secret ARNs keyed by role."
  value       = module.secret_store.database_secret_arns
}

output "role_bootstrap_function_name" {
  description = "Function that inv db-bootstrap-roles invokes to create the per-service database roles and databases."
  value       = module.secret_store.role_bootstrap_function_name
}

output "rebar_ticket_store_bucket" {
  description = "Ticket store bucket, set as store.bucket in tickets.yaml. Null when create_rebar_ticket_store is false."
  value       = module.rebar_ticket_store.bucket
}

output "rebar_ticket_store_reader_role_arn" {
  description = "Role the reader profile assumes, set as roles.read_only in tickets.yaml."
  value       = module.rebar_ticket_store.reader_role_arn
}

output "rebar_ticket_store_writer_role_arn" {
  description = "Role the writer profile assumes, set as roles.write in tickets.yaml."
  value       = module.rebar_ticket_store.writer_role_arn
}

output "ecs_cluster_arn" {
  description = "Platform ECS cluster ARN. Empty when create_ecs_cluster is false."
  value       = module.ecs_cluster.cluster_arn
}

output "ecs_cluster_name" {
  description = "Platform ECS cluster name."
  value       = module.ecs_cluster.cluster_name
}

output "ecs_deployer_policy_arn" {
  description = "Policy a team attaches to its deploy role to deploy services into the platform ECS cluster."
  value       = module.ecs_cluster.deployer_policy_arn
}

output "ecs_exec_task_policy_arn" {
  description = "Policy a task role attaches to accept ECS Exec sessions in the platform ECS cluster."
  value       = module.ecs_cluster.exec_task_policy_arn
}

output "container_repository_urls" {
  description = "ECR repository of each platform container image, keyed by the name inv publish-platform-images uses."
  value       = module.container_services.repository_urls
}

output "container_deploy" {
  description = "What inv platform-deploy runs."
  value       = module.container_services.deploy
}

output "container_workspace" {
  description = "Workspace file system and its checkouts and index access points."
  value       = module.container_services.workspace
}

output "container_clickhouse" {
  description = "The ClickHouse TLS secret inv clickhouse-certs put writes, and the name its certificate must carry."
  value       = module.container_services.clickhouse
}

output "container_telemetry" {
  description = "The Prometheus workspace the collector writes to, the X-Ray encryption key the module set, and the collector URL."
  value       = module.container_services.telemetry
}

output "container_langfuse" {
  description = "The internal Langfuse URL the platform services export to, and the URL inv langfuse-ui forwards."
  value       = module.container_services.langfuse
}

output "container_internal_endpoints" {
  description = "The internal load balancer's client security group, each service's target security group and target group, and each service's HTTPS URL."
  value       = module.container_services.internal_endpoints
}

output "temporal_server_repository_url" {
  description = "ECR repository that inv publish-temporal-images pushes the Temporal server image to."
  value       = module.temporal_control_plane.server_repository_url
}

output "temporal_admin_tools_repository_url" {
  description = "ECR repository that inv publish-temporal-images pushes the Temporal admin-tools image to."
  value       = module.temporal_control_plane.admin_tools_repository_url
}

output "temporal_tls" {
  description = "Temporal TLS secrets and certificate sources that inv temporal-certs put reads."
  value = {
    frontend_certificate_source   = module.temporal_control_plane.frontend_certificate_source
    ca_secret_arn                 = module.temporal_control_plane.tls_ca_secret_arn
    internode_secret_arn          = module.temporal_control_plane.tls_internode_secret_arn
    frontend_secret_arn           = module.temporal_control_plane.tls_frontend_secret_arn
    frontend_acm_certificate_arn  = module.temporal_control_plane.frontend_acm_certificate_arn
    internode_acm_certificate_arn = module.temporal_control_plane.internode_acm_certificate_arn
    certificate_expiry_topic_arn  = module.temporal_control_plane.certificate_expiry_topic_arn
    frontend_server_name          = module.temporal_control_plane.frontend_server_name
    internode_server_name         = module.temporal_control_plane.internode_server_name
  }
}

output "temporal_ui_repository_url" {
  description = "ECR repository that inv publish-temporal-images pushes the Temporal UI image to."
  value       = module.temporal_control_plane.ui_repository_url
}

output "temporal_client" {
  description = "Settings a Temporal client uses: TEMPORAL_HOST, TEMPORAL_NAMESPACE, TEMPORAL_TLS_SERVER_NAME, the secret to inject as TEMPORAL_TLS_CA, and the security group to attach."
  value = {
    endpoint          = module.temporal_control_plane.endpoint
    namespace         = module.temporal_control_plane.namespace
    server_name       = module.temporal_control_plane.frontend_server_name
    ca_secret_arn     = module.temporal_control_plane.client_ca_secret_arn
    security_group_id = module.temporal_control_plane.client_security_group_id
  }
}

output "temporal_ui_url" {
  description = "HTTPS address of the Temporal UI load balancer, reachable from the management tier."
  value       = module.temporal_control_plane.ui_url
}

output "temporal_deploy" {
  description = "What inv temporal-deploy runs."
  value       = module.temporal_control_plane.deploy
}

output "off_hours" {
  description = "The automation documents that put the services to sleep and wake them, the role they assume, and the parameter holding the counts the wake restores."
  value = {
    documents             = module.off_hours.documents
    automation_role_arn   = module.off_hours.automation_role_arn
    wake_counts_parameter = module.off_hours.wake_counts_parameter
  }
}
