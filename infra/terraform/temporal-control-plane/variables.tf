variable "create_temporal_control_plane" {
  description = "Create the Temporal control plane, starting with the ECR repositories for the server and admin-tools images."
  type        = bool
  default     = false
}

variable "name_prefix" {
  description = "Prefix for the repository and resource names."
  type        = string
}

variable "kms_key_arn" {
  description = "Customer-managed KMS key ARN that encrypts the image repositories. Empty creates one key per repository."
  type        = string
  default     = ""
}

variable "tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = {}
}

variable "frontend_acm_certificate_arn" {
  description = "ACM certificate the NLB presents to Temporal clients, issued by ACM or imported into it. Empty serves the frontend certificate that inv temporal-certs put writes to Secrets Manager."
  type        = string
  default     = ""

  validation {
    condition     = var.frontend_acm_certificate_arn == "" || can(regex("^arn:aws[a-z-]*:acm:[a-z0-9-]+:[0-9]{12}:certificate/[0-9a-f-]+$", var.frontend_acm_certificate_arn))
    error_message = "frontend_acm_certificate_arn must be empty or an ACM certificate ARN."
  }
}

variable "internode_acm_certificate_arn" {
  description = "ACM certificate that tracks the internode certificate, imported by inv temporal-certs put --import-to-acm or by the operator. The server reads the internode certificate from Secrets Manager, because ACM never returns the key of an imported certificate, and put refuses a certificate that does not match this one."
  type        = string
  default     = ""

  validation {
    condition     = var.internode_acm_certificate_arn == "" || can(regex("^arn:aws[a-z-]*:acm:[a-z0-9-]+:[0-9]{12}:certificate/[0-9a-f-]+$", var.internode_acm_certificate_arn))
    error_message = "internode_acm_certificate_arn must be empty or an ACM certificate ARN."
  }
}

variable "tls_secret_readers" {
  description = "IAM role ARNs allowed to read the TLS secrets. Every other principal is denied GetSecretValue."
  type        = list(string)
  default     = []
}

variable "secret_recovery_window_days" {
  description = "Days a deleted TLS secret stays recoverable. Set 0 in a disposable environment that is destroyed and applied again."
  type        = number
  default     = 30

  validation {
    condition     = var.secret_recovery_window_days == 0 || (var.secret_recovery_window_days >= 7 && var.secret_recovery_window_days <= 30)
    error_message = "secret_recovery_window_days must be 0 or between 7 and 30."
  }
}
