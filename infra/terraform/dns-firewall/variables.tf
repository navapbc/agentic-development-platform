variable "create_dns_firewall" {
  description = "Master toggle. When false the module provisions nothing, for networks whose organization already filters DNS."
  type        = bool
  default     = false
}

variable "name_prefix" {
  description = "Prefix for the created resource names."
  type        = string
}

variable "vpc_id" {
  description = "VPC whose Route 53 Resolver queries are filtered."
  type        = string
  default     = ""
}

variable "allowed_domains" {
  description = "Domains the VPC may resolve. A leading `*.` matches subdomains only, so list the apex separately when it is also needed. AWS service names resolve under amazonaws.com and must stay allowed."
  type        = list(string)
  default = [
    "amazonaws.com",
    "*.amazonaws.com",
    "*.internal",
    "github.com",
    "*.github.com",
    "*.githubusercontent.com",
    "*.atlassian.net",
    "atlassian.com",
    "*.atlassian.com",
    "hooks.slack.com",
  ]
}

variable "additional_allowed_domains" {
  description = "Domains appended to allowed_domains, such as the SMTP relay host or a private service-discovery namespace."
  type        = list(string)
  default     = []
}

variable "default_action" {
  description = "Action for a query outside the allowlist. BLOCK answers NXDOMAIN. ALERT resolves the query and logs it, for a trial run before enforcing."
  type        = string
  default     = "BLOCK"

  validation {
    condition     = contains(["BLOCK", "ALERT"], var.default_action)
    error_message = "default_action must be BLOCK or ALERT."
  }
}

variable "query_log_retention_days" {
  description = "CloudWatch retention for the resolver query log. Must be a value CloudWatch Logs accepts."
  type        = number
  default     = 365
}

variable "kms_key_arn" {
  description = "KMS key ARN for the query log group. Empty uses the CloudWatch Logs default encryption."
  type        = string
  default     = ""
}

variable "tags" {
  description = "Tags applied to the created resources."
  type        = map(string)
  default     = {}
}
