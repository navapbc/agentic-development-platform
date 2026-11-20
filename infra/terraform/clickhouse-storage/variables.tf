variable "create_storage" {
  description = "Master toggle. When false the module provisions nothing, so existing ClickHouse storage can be reused."
  type        = bool
  default     = false
}

variable "name_prefix" {
  description = "Prefix for the file system and supporting resource names."
  type        = string
}

variable "vpc_id" {
  description = "VPC the file system serves."
  type        = string
  default     = ""
}

variable "subnet_ids" {
  description = "Data-tier subnets that receive a mount target, at most one per Availability Zone."
  type        = list(string)
  default     = []
}

variable "client_security_group_ids" {
  description = "Security groups of the ClickHouse task, admitted to NFS."
  type        = list(string)
  default     = []
}

variable "client_cidr_blocks" {
  description = "CIDRs admitted to NFS. Pass the application tier CIDRs, where the ClickHouse task runs."
  type        = list(string)
  default     = []
}

variable "throughput_mode" {
  description = "EFS throughput mode. elastic scales with ClickHouse merge bursts and bills per use."
  type        = string
  default     = "elastic"

  validation {
    condition     = contains(["bursting", "elastic"], var.throughput_mode)
    error_message = "throughput_mode must be bursting or elastic."
  }
}

variable "posix_uid" {
  description = "POSIX user the access point enforces. 101 is the clickhouse user in the official image."
  type        = number
  default     = 101
}

variable "posix_gid" {
  description = "POSIX group the access point enforces. 101 is the clickhouse group in the official image."
  type        = number
  default     = 101
}

variable "kms_key_arn" {
  description = "KMS key ARN for at-rest encryption. Empty uses the AWS managed aws/elasticfilesystem key."
  type        = string
  default     = ""
}

variable "tags" {
  description = "Tags applied to the created resources."
  type        = map(string)
  default     = {}
}
