variable "create_managed_grafana" {
  description = "Create the Grafana workspace, its workspace role, the workspace security group, the workspace KMS key, and, with operator_principal_arns, the Prometheus query role. When false the module provisions nothing."
  type        = bool
  default     = false
}

variable "name_prefix" {
  description = "Prefix for the workspace, role, security group, and key alias names."
  type        = string
}

variable "prometheus_workspace_arn" {
  description = "ARN of the Amazon Managed Service for Prometheus workspace the workspace role and the query role may query."
  type        = string
  default     = ""
  nullable    = false
}

variable "prometheus_endpoint" {
  description = "Endpoint of the Prometheus workspace, ending in a slash, which the Prometheus data source queries."
  type        = string
  default     = ""
  nullable    = false
}

variable "xray_kms_key_arn" {
  description = "KMS key the region's X-Ray encryption uses, which the workspace role may decrypt with to read traces. Empty when X-Ray uses its default encryption."
  type        = string
  default     = ""
  nullable    = false
}

variable "operator_principal_arns" {
  description = "IAM principals that may assume the Prometheus query role, such as an operator SSO permission set role. Empty creates no query role."
  type        = list(string)
  default     = []
}

variable "vpc_id" {
  description = "VPC the workspace connects to for its outbound traffic."
  type        = string
  default     = ""
}

variable "subnet_ids" {
  description = "Application tier subnets in at least two Availability Zones, each with 15 free addresses, where the workspace places its network interfaces."
  type        = list(string)
  default     = []
}

variable "endpoints_security_group_id" {
  description = "Security group of the VPC interface endpoints, the only destination the workspace may reach."
  type        = string
  default     = ""
}

variable "allowed_vpc_endpoint_ids" {
  description = "grafana-workspace interface endpoints that may reach the workspace. Empty admits any network, and IAM Identity Center still authenticates every user."
  type        = list(string)
  default     = []
}

variable "admin_group_ids" {
  description = "IAM Identity Center group IDs granted the Grafana Admin role, which configures the data sources."
  type        = list(string)
  default     = []
}

variable "editor_group_ids" {
  description = "IAM Identity Center group IDs granted the Grafana Editor role."
  type        = list(string)
  default     = []
}

variable "viewer_group_ids" {
  description = "IAM Identity Center group IDs granted the Grafana Viewer role."
  type        = list(string)
  default     = []
}

variable "grafana_version" {
  description = "Grafana version of the workspace."
  type        = string
  default     = "10.4"
}

variable "tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = {}
}
