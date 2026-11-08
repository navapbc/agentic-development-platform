variable "create_eks_deployer" {
  description = "Create the eks-deployer one-shot task, its roles, its security group, and its log group. The task definition needs image as well."
  type        = bool
  default     = false
}

variable "name_prefix" {
  description = "Prefix for the task family, role, security group, key alias, and log group names."
  type        = string
}

variable "image" {
  description = "eks-deployer image reference, which inv publish-platform-images writes. Empty creates no task definition."
  type        = string
  default     = ""
}

variable "repository_arn" {
  description = "ECR repository of the image, which the execution role may pull from."
  type        = string
  default     = ""
}

variable "cluster_arn" {
  description = "ECS cluster the task runs on."
  type        = string
  default     = ""
}

variable "vpc_id" {
  description = "VPC of the task's security group."
  type        = string
  default     = ""
}

variable "subnet_ids" {
  description = "Subnets the task runs in, the management tier."
  type        = list(string)
  default     = []
}

variable "endpoints_security_group_id" {
  description = "Security group of the VPC interface endpoints, which the task reaches for ECR and CloudWatch Logs."
  type        = string
  default     = ""
}

variable "deploy_inputs" {
  description = "JSON the task reads from DEPLOY_INPUTS, holding the environment's images, settings, security groups, secret ARNs, and network ranges. It carries no secret value."
  type        = string
  default     = "{}"
}

variable "log_retention_days" {
  description = "Retention for the task log group. Must be a value CloudWatch Logs accepts."
  type        = number
  default     = 365
}

variable "tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = {}
}
