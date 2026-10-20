variable "create_jumphost" {
  description = "Master toggle. When false the module provisions nothing, so it can be wired into a root module and left off by default."
  type        = bool
  default     = false
}

variable "region" {
  description = "AWS region hosting the jumphost cluster and endpoints."
  type        = string
  default     = "us-east-1"
}

variable "vpc_id" {
  description = "VPC in which to run the Fargate task and, optionally, the endpoints."
  type        = string
  default     = ""
}

variable "subnet_ids" {
  description = "Private subnets for the Fargate task ENI and the interface endpoints."
  type        = list(string)
  default     = []
}

variable "jumphost_name" {
  description = "Logical jumphost name. Also the SSM parameter segment the connect script resolves."
  type        = string
  default     = "bedrock-jumphost"
}

variable "jumphost_env" {
  description = "Optional environment segment for the SSM parameter path. Empty for a single-environment account."
  type        = string
  default     = ""
}

variable "image_uri" {
  description = "Full image reference the task runs, for example <ecr-url>:v1 or <ecr-url>@sha256:<digest>."
  type        = string
  default     = ""
}

variable "ecr_repository_arn" {
  description = "ARN of the ECR repository the execution role may pull from."
  type        = string
  default     = ""
}

variable "cpu" {
  description = "Fargate task CPU units."
  type        = string
  default     = "256"
}

variable "memory" {
  description = "Fargate task memory in MiB."
  type        = string
  default     = "512"
}

variable "cpu_architecture" {
  description = "Task CPU architecture. ARM64 is cheaper. Build the image for the same architecture."
  type        = string
  default     = "ARM64"
}

variable "duration_in_secs" {
  description = "Task self-termination TTL. The connect script can override it per session."
  type        = number
  default     = 3600
}

variable "endpoints_security_group_id" {
  description = "Security group id of the shared VPC endpoints. The task is allowed 443 egress to it."
  type        = string
  default     = ""
}

variable "attach_endpoints_egress" {
  description = "Add a task egress rule to endpoints_security_group_id. Set false when the task reaches the endpoints by CIDR instead."
  type        = bool
  default     = true
}

variable "attach_s3_egress" {
  description = "Add a task egress rule to the region's S3 managed prefix list, required to pull ECR image layers through the S3 gateway endpoint."
  type        = bool
  default     = true
}

variable "additional_egress_cidrs" {
  description = "Extra CIDR blocks the task may reach on 443, for a caller that reaches the endpoints by CIDR rather than security group."
  type        = list(string)
  default     = []
}

variable "log_retention_days" {
  description = "Retention for the ECS Exec CloudWatch log group."
  type        = number
  default     = 365
}

variable "access_log_bucket" {
  description = "Target bucket for S3 server access logs on the exec-log bucket, such as the bucket the access-logs module creates."
  type        = string

  validation {
    condition     = length(trim(var.access_log_bucket, " ")) > 0
    error_message = "access_log_bucket must name the bucket that receives S3 server access logs."
  }
}

variable "logs_bucket_kms_key_arn" {
  description = "KMS key ARN for the exec log bucket. Empty uses SSE-S3 (AES256), acceptable for a test account."
  type        = string
  default     = ""
}

variable "logs_bucket_principal_list" {
  description = "Principal ARNs allowed to read objects in the exec log bucket, in addition to the account."
  type        = list(string)
  default     = []
}

variable "secrets" {
  description = "Secrets Manager values to inject into the task, each an object of name and secret_arn. Empty for a Bedrock port-forward, which needs none."
  type = list(object({
    name       = string
    secret_arn = string
  }))
  default = []
}

variable "permissions_boundary_arn" {
  description = "Permissions boundary applied to both task roles. Empty in a standard account, set to the delegated-admin boundary in CMS accounts."
  type        = string
  default     = ""
}

variable "iam_path" {
  description = "IAM path for the task roles. Set to /delegatedadmin/developer/ in CMS accounts."
  type        = string
  default     = "/"
}

variable "name_prefix" {
  description = "Name prefix for created resources."
  type        = string
  default     = "agentic-platform"
}

variable "tags" {
  description = "Tags applied to all created resources."
  type        = map(string)
  default     = {}
}
