variable "egress_mode" {
  description = "Outbound internet path for the private tiers. none adds no path, for networks whose organization already provides egress. nat_instance runs a hardened NAT instance for low-cost testing. nat_gateway uses a managed NAT Gateway for production."
  type        = string
  default     = "none"

  validation {
    condition     = contains(["none", "nat_instance", "nat_gateway"], var.egress_mode)
    error_message = "egress_mode must be none, nat_instance, or nat_gateway."
  }
}

variable "name_prefix" {
  description = "Prefix for the created resource names."
  type        = string
}

variable "vpc_id" {
  description = "VPC the egress path serves."
  type        = string
  default     = ""
}

variable "public_subnet_id" {
  description = "Public subnet, routed to an internet gateway, that hosts the NAT instance or NAT Gateway. Required unless egress_mode is none."
  type        = string
  default     = ""
}

variable "private_route_table_ids" {
  description = "Route tables that receive a default route through the egress path. Pass the application and management tier tables only. The data tier never receives one."
  type        = list(string)
  default     = []
}

variable "source_cidr_blocks" {
  description = "CIDRs whose traffic the NAT instance forwards and translates."
  type        = list(string)
  default     = []
}

variable "allowed_egress_ports" {
  description = "TCP ports the NAT instance forwards to the internet. 443 covers GitHub, Atlassian, and Slack. 587 covers SMTP submission."
  type        = list(number)
  default     = [443, 587]
}

variable "nat_instance_type" {
  description = "Instance type of the NAT instance. Must match the architecture of nat_instance_ami_ssm_parameter."
  type        = string
  default     = "t4g.nano"
}

variable "nat_instance_ami_ssm_parameter" {
  description = "Public SSM parameter that resolves the NAT instance AMI. The default is Amazon Linux 2023 minimal for arm64."
  type        = string
  default     = "/aws/service/ami-amazon-linux-latest/al2023-ami-minimal-kernel-default-arm64"
}

variable "kms_key_arn" {
  description = "KMS key ARN for the NAT instance root volume. Empty uses the account default EBS key."
  type        = string
  default     = ""
}

variable "tags" {
  description = "Tags applied to the created resources."
  type        = map(string)
  default     = {}
}
