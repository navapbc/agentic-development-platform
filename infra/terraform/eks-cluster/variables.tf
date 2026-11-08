variable "create_eks_cluster" {
  description = "Create the EKS cluster, its KMS keys, its control plane log group, its node groups, its add-ons, and its Pod Identity roles."
  type        = bool
  default     = false
}

variable "name_prefix" {
  description = "Prefix for the cluster, key alias, log group, role, and node group names."
  type        = string
}

variable "cluster_name" {
  description = "Cluster name. Defaults to <name_prefix>-eks."
  type        = string
  default     = ""
}

variable "kubernetes_version" {
  description = "Kubernetes minor version of the control plane and the managed node groups."
  type        = string
  default     = "1.37"
}

variable "subnet_ids" {
  description = "Private subnets, in at least two Availability Zones, for the control plane network interfaces and the nodes."
  type        = list(string)
  default     = []
}

variable "api_access_security_group_ids" {
  description = "Security groups admitted to the private API endpoint on 443, such as the operator jumphost."
  type        = list(string)
  default     = []
}

variable "admin_principal_arns" {
  description = "IAM principals granted AmazonEKSClusterAdminPolicy through access entries, keyed by a static label so the keys are known before the principals exist."
  type        = map(string)
  default     = {}
}

variable "platform_node_group" {
  description = "Instance types and scaling of the platform node group, which runs the platform workloads and the cluster add-ons. The instance types must be Graviton, because the images are arm64."
  type = object({
    instance_types = optional(list(string), ["m7g.large"])
    min_size       = optional(number, 1)
    max_size       = optional(number, 3)
    desired_size   = optional(number, 2)
    disk_size_gib  = optional(number, 50)
  })
  default = {}

  validation {
    condition     = alltrue([for type in var.platform_node_group.instance_types : can(regex("^[a-z]+[0-9]+g[a-z]*\\.", type))])
    error_message = "platform_node_group.instance_types must be Graviton (arm64) instance types, such as m7g.large."
  }
}

variable "sandbox_node_group" {
  description = "Instance types and scaling of the sandbox node group, which is tainted so that only the sandbox, gate, checks, and scanner pods that tolerate the taint run on it. The instance types must be Graviton."
  type = object({
    instance_types = optional(list(string), ["m7g.large"])
    min_size       = optional(number, 1)
    max_size       = optional(number, 3)
    desired_size   = optional(number, 1)
    disk_size_gib  = optional(number, 100)
  })
  default = {}

  validation {
    condition     = alltrue([for type in var.sandbox_node_group.instance_types : can(regex("^[a-z]+[0-9]+g[a-z]*\\.", type))])
    error_message = "sandbox_node_group.instance_types must be Graviton (arm64) instance types, such as m7g.large."
  }
}

variable "network_policy_enforcing_mode" {
  description = "VPC CNI network policy mode. standard starts a new pod open until its policies are programmed. strict starts every pod denied and keeps a pod that no NetworkPolicy selects denied, so set strict only after the kube-system and platform policies are applied."
  type        = string
  default     = "standard"

  validation {
    condition     = contains(["standard", "strict"], var.network_policy_enforcing_mode)
    error_message = "network_policy_enforcing_mode must be standard or strict."
  }
}

variable "use_fips_endpoints" {
  description = "Have the Secrets Store CSI provider call the FIPS endpoint of Secrets Manager."
  type        = bool
  default     = false
}

variable "addon_versions" {
  description = "Add-on version by add-on name, such as { coredns = \"v1.14.7-eksbuild.35\" }. An add-on not listed gets the default version EKS publishes for the cluster version."
  type        = map(string)
  default     = {}
}

variable "log_retention_days" {
  description = "Retention for the control plane log group. Must be a value CloudWatch Logs accepts."
  type        = number
  default     = 365
}

variable "platform_namespace" {
  description = "Namespace of the activity-worker and litellm service accounts."
  type        = string
  default     = "agentic-dev-platform"
}

variable "activity_worker_policy_arns" {
  description = "IAM policies attached to the activity-worker Pod Identity role."
  type        = list(string)
  default     = []
}

variable "litellm_policy_arns" {
  description = "IAM policies attached to the litellm Pod Identity role, such as the bedrock:InvokeModel policy of bedrock-iam."
  type        = list(string)
  default     = []
}

variable "efs_csi_node_policy_arns" {
  description = "IAM policies attached to the EFS CSI node role, which mounts the file systems with IAM authorization, such as a policy granting elasticfilesystem:ClientMount and ClientWrite on an access point."
  type        = list(string)
  default     = []
}

variable "tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = {}
}
